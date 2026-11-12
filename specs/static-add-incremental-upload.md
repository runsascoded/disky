# `static-names add`: upload after each scan, not once at the end

From disky-m3, 2026-11-11. `[cloud]` (`cloud/src/dt_cloud/static_local.py`, `add_cmd`).

## Problem

`static-names add` over a URL store appends every pending scan in one loop (`append_scan` per scan, then `merge_due`), and only then calls `upload_new(store, gen_dir, prefix, before)`. A failure anywhere in the loop (OOM, Spot interruption, job timeout) discards every scan appended so far; the next run starts from the same newest manifest and redoes them all.

This is harmless while each run has one new scan, but once a backlog forms it can never drain: m3's store stuck at `2026-10-18T1014` while ~50 scans accumulated. Every ingest since spent ~3.6 h appending ~32 scans, then hit DuckDB OOM on the 11/02 scan's anchor rollups (`static_anchors.run_rollups`, 4.6 GiB cap) or the 4 h job timeout, uploading nothing.

Affected: every deployment on the one-machine path (m3, hf, r2, and all `*-meta` stores). gcs and cw primaries use the staged Batch path (`static-names: {profile: …}`), which persists per stage.

## Change

After each `append_scan` (and any carries due at that point), publish what's new before moving on: `upload_new` with the usual order (data → `meta.json`s → manifests), then advance `before`. The manifest upload is the commit point, so a failure mid-scan leaves the previous scan's manifest as the newest and the next run resumes there. Keep the final prune behavior unchanged.

Equivalent alternative if per-scan uploads are too chatty for a large backlog: upload every N scans or every T seconds; per-scan is simplest and m3's per-scan publish is 1–20 min of work vs. seconds of upload.

## Also: DuckDB threads on Fargate

`scan_steps.static_names` passes `-p ctx.nproc`, and `Ctx.nproc` = `min(affinity, cpu_quota(...))`. On Fargate `cpu.max` reads `max` (the quota is the VM's, not the cgroup's), so `cpu_quota` returns None and `nproc` is the host's CPU count: m3's 2-vCPU task ran `static-names add -p 8 -M 9GB`. Eight threads' working buffers inside one memory cap make DuckDB's OOM more likely, and add contention on 2 vCPUs. Other steps take `ctx.nproc` too. Suggested fix: a `threads` option on the step (and/or an env override such as `DT_NPROC`, set by each deployment's job def from its vCPUs), since the cgroup can't tell us.

## Test

A fake store whose second `append_scan` raises: after the failure, the store's newest manifest is the first scan's, and a rerun appends only the remaining scans (assert exact manifest key lists before/after).

## m3 status

m3's 8 h catch-up ingest (job `889a6b5c`, 2026-11-12) appended 33 scans in 3.8 h, then hit the same OOM (`run_rollups`, `ck` table, 8.3 GiB cap, `-p 8`) on scan `2026-11-02T2311`, and uploaded nothing. m3 is now draining the backlog with a one-off image mode (`INGEST_PIPELINE=static-catchup`: `static-names add -t <id>` per chunk, explicit `-p`/`-M`, on a 4 vCPU / 30 GiB task); it does not depend on this change.
