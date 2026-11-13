# `static-names add`: upload after each scan, not once at the end

From disky-m3, 2026-11-11. `[cloud]` (`cloud/src/dt_cloud/static_local.py`, `add_cmd`). Done 2026-11-12 (as built below).

## Problem

`static-names add` over a URL store appends every pending scan in one loop (`append_scan` per scan, then `merge_due`), and only then calls `upload_new(store, gen_dir, prefix, before)`. A failure anywhere in the loop (OOM, Spot interruption, job timeout) discards every scan appended so far; the next run starts from the same newest manifest and redoes them all.

This is harmless while each run has one new scan, but once a backlog forms it can never drain: m3's store stuck at `2026-10-18T1014` while ~50 scans accumulated. Every ingest since spent ~3.6 h appending ~32 scans, then hit DuckDB OOM on the 11/02 scan's anchor rollups (`static_anchors.run_rollups`, 4.6 GiB cap) or the 4 h job timeout, uploading nothing.

Affected: every deployment on the one-machine path (m3, hf, r2, and all `*-meta` stores). gcs and cw primaries use the staged Batch path (`static-names: {profile: …}`), which persists per stage.

## Change (as built)

`add_cmd` loops over the pending scans; for each one:
1. `append_scan` (its run, published locally by `manifests/<id>.json`);
2. `publish()`: `upload_new(store, gen_dir, prefix, before)` in the usual order (data → `meta.json`s → manifests), then `before` gains the uploaded keys (with their local sizes), so the next publish skips them;
3. unless `-N`: `merge_due` (the carries due at that point, each a manifest revision), then `publish()` again.

The manifest upload is the commit point: a failure on a scan (in its append or its carries) leaves every earlier scan's manifest in the store, the newest of them the last one published, and the next `add` resumes after it. Publishing between the append and its carries (rather than once after both) means a failed carry doesn't take the scan's append with it; the carry is redone by the next `add` (`merge_pending` plans from the newest manifest). Carries now run per scan, as successive single-scan `add`s would, instead of once after the whole backlog. The final prune and the compaction check are unchanged.

The summary's `merge` is now one `merge_pending` result per appended scan (`null` when nothing was pending, or `-N`); `files` counts every key uploaded across the publishes.

Not done: batching (every N scans or T seconds). Per-scan is simplest, and m3's per-scan work (1–20 min) dwarfs its upload (seconds).

## Also: DuckDB threads on Fargate

As built: `Ctx.nproc` honors `$DT_NPROC` (`scan_steps.NPROC_ENV`) first, a positive integer (anything else fails the step, `StepFailed` rc 2), else affinity capped by the cgroup quota as before; every `ctx.nproc` and `{nproc}` user follows it. A deployment's job definition sets it from its vCPUs (generic code ships no default). The one-machine `static-names` step also takes `options.static-names.threads` (`-p`, default `ctx.nproc`; only with `gen`, like `mem`), mirroring `options.index.threads`. Tests: `test_nproc_env_overrides_affinity_and_quota`, `test_static_names_one_machine` (`threads` case), `test_static_names_profile_checks`.

The problem, as reported: `scan_steps.static_names` passes `-p ctx.nproc`, and `Ctx.nproc` = `min(affinity, cpu_quota(...))`. On Fargate `cpu.max` reads `max` (the quota is the VM's, not the cgroup's), so `cpu_quota` returns None and `nproc` is the host's CPU count: m3's 2-vCPU task ran `static-names add -p 8 -M 9GB`. Eight threads' working buffers inside one memory cap make DuckDB's OOM more likely, and add contention on 2 vCPUs. Other steps take `ctx.nproc` too. Suggested fix: a `threads` option on the step (and/or an env override such as `DT_NPROC`, set by each deployment's job def from its vCPUs), since the cgroup can't tell us.

## Test

`test_static_local.py::test_add_publishes_each_scan_before_the_next`: a base built through the fourth-newest scan, three scans pending, `append_scan` patched to raise on its second call. After the failure the store's manifests are exactly `[<a>.json]` and its runs `[deltas/<a>]`; a rerun (a fresh work dir, as the next job's) appends exactly `[b, c]` after `a`, its carries `[[deltas/<a>_<b>], []]`, leaving manifests `[<a>.json, <b>.json, <b>.m001.json, <c>.json]` with `<c>`'s runs `[(deltas/<a>_<b>, 1), (deltas/<c>, 0)]`. It failed on the old code (one upload after the loop: no manifest in the store after the failure) and passes with the change.

## m3 status

m3's 8 h catch-up ingest (job `889a6b5c`, 2026-11-12) appended 33 scans in 3.8 h, then hit the same OOM (`run_rollups`, `ck` table, 8.3 GiB cap, `-p 8`) on scan `2026-11-02T2311`, and uploaded nothing. m3 is now draining the backlog with a one-off image mode (`INGEST_PIPELINE=static-catchup`: `static-names add -t <id>` per chunk, explicit `-p`/`-M`, on a 4 vCPU / 30 GiB task); it does not depend on this change.
