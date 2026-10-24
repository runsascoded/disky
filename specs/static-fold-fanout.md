# Static compaction: the drill fold fanned out over Batch tasks

Status: built (phase 1) on branch `fold-fanout`: the lazy `spilled` restore (its own commit), then `FoldFanout` (classes from the persisted digests of step 3, else a digest wave), the `fold-shard` stage, `fold_tasks` / `compact -L`. Not yet run on gcs. Phase 2 (below) not built. Siblings: [static-measure-fanout.md] (built), [static-anchors-fanout.md] (built); related: [static-incremental-compaction.md] (step 3 supplies a class source), [static-compaction-perf.md] (lever 4).

## Why

With measure (~40 min) and anchors (~30 min) fanned out, the drill fold is the long pole of a from-scratch compaction. gcs's c001 (2026-10-24, n2-highmem-48, `-M 270GB -p 48 -j 8`):

| step | s | what |
|---|---|---|
| drill/fold/plan | 7 | 47,632 tuples, 4,881 chunks |
| drill/fold/runs-spill | 216 | the runs' long roots by chunk |
| drill/fold/long-roots | 3,054 | 40.1 B rows; disk peak 1,019 GiB, RSS 322 GiB |
| long-roots checkpoint upload | 966 | blocking then, backgrounded since |
| drill/fold/aliases | ~7 | |
| drill/fold/canonical-roots | 2,054 | lcan 39.55 B, 5,371 ranges |
| drill/fold/short-roots | 1,471 | sall 15.3 B, 776 ranges |
| `spilled` checkpoint upload | 1,463 | 730 GB (32-process uploader now: ~1.5 GB/s) |

That is 6.8 ks (1.9 h) of fold compute on one VM, plus 2.4 ks of uploads. Goal: from-scratch compaction ≲ 2 h. That needs the fold at ~30 min.

## The current fold, as units

`fold_drill` (`static_compact.py:250`) runs `_long_roots` (`:358`), then `_spill_roots` (`:477`), then saves `spilled` (`:313`):

1. **Plan (global, cheap).** Each member's tuple of per-tier classes `mt`; distinct tuples `tup` (`tid` ordered by the base's class `c0`, `:372`); chunks `tch` of ~`chunk_rows` estimated rows from the roots indexes (`:382`). A chunk is `(ch, tid a..b, c0 lo..hi)`.
2. **Runs' spill (global).** Every run tier's long roots joined to `tup`/`tch`, into a `ChunkSpill` of ≤ 64 partitions of consecutive chunks (`:388`, `static_roots.py:1113`).
3. **Long roots (per chunk).** The base's roots files whose `q` span overlaps `[lo, hi]` (`q_spans`, `:403`) plus the chunk's runs spill are combined (`GROUP BY tid, path, usr, vf`, min `vt`) into `ct`. Each tid's digest `(n, h1, h2)` goes to `tdig`, and `ct` goes to `troots/c<ch>.parquet` (`:409`).
4. **Classes (global barrier).** `al(q, canonical)`: `min(q)` over members with equal digests (`:484`). Equal digests can sit in different tuples, hence different chunks, so every chunk's digests must be in first. Then `ctid` (canonical → tid) and the long `q`-range plan `qg` (`:492`).
5. **Canonical roots (per chunk file).** `troots/c<ch>` ⋈ `ctid` → rows keyed by canonical `q` + `depth`, spilled to `work/long/fold<k>/grp=<g>/` (`:496`).
6. **Short roots (per short range, independent of `al`).** Range `g`'s literals from every tier's short roots by span, combined, spilled to `work/short/fold<g>/grp=<g>/` (`:527`). The short plan's bounds are the measure ranges' bounds (`q_bounds` and `plan_groups` over the same counts, `:522`), so short unit `g` writes exactly measure range `g`.

Measure reads `work/<kind>/<producer>/grp=<g>/*.parquet` (`static_local.spill_files`, `static_local.py:473`). It re-sorts each range on its unique key `(q, path, usr, vf)` (`_measure_one`, `static_local.py:418`). Its `dirs`/`rup` consumers sort too. So the measured bytes depend only on each range's row set, not on which producer wrote which file. That is the property the fan-out relies on, and the 1-vs-N test pins it.

## Shape

The unit is **one chunk's spill, parameterised by the class map `al`**: a `long` unit (`static_compact.fold_units`) combines the chunk's rows (`_combine`: step 3's query), keeps only canonical tuples (`ctid`), and writes them by canonical `q` range (steps 3+5 fused, no `troots` intermediate). Short ranges are units too (`short`, `_short_one`: needs no `al`).

`al` is derived from `tdig (tid, n, h1, h2)` either way (`_classes`: the existing SQL). A pluggable **class source** produces `tdig` (`static_fanout`: `wave(chunks)` = the units that must run first, `tdig(con, fan, chunks, dig_dir)`):

- **`PersistedClasses` (the default).** [static-incremental-compaction.md] step 3 (on `cloud`): generations write `drill/digests-long.parquet` `(q, n, h1, h2)` per member, runs per-class deltas marked `digests: delta`; `_derive_digests` (`_tuple_digests`) sums base + each tier's deltas into `tdig` before any root is read. No digest wave: wave 2 also carries the short ranges and is the only wave. Each of its `long` units checks its tuples' counts against the roots it combined (`_check_counts`, as the single-VM fold's chunks do) and records mismatches (`done.json` `bad`, `fix.parquet`: their digests from the roots). After the wave the fold VM corrects `tdig` (`_fix_digests`), recomputes the classes, and if `al`, `ctid` or `lqg` changed, spills every chunk again (wave 3, `relong` units; the classes step's `respill`); the result's `fixed` counts the members corrected. Where `digests_blocker` refuses (c001 and other bases or runs built before the digests), it falls back to the digest wave.
- **`DigestClasses` (the fallback; the path for the next compaction or two, until the bases carry their digests).** Needs every long chunk combined once first, to digest it. Its wave-1 unit is a `digest` chunk (combine and digest, no `troots` write; `folded/digest/c<ch>/dig.parquet`), gathered by `gather_digests`.
- Not done: the derived exact `n` per tuple as the plan's row estimate, and dropping the non-canonical tuples from the chunks (47,632 → ~45 K tuples, never combined).

### Waves

Opt-in per profile: `fold_tasks: N` (`$STATIC_NAMES_FOLD_TASKS`, default 1 = today's path). `compact_jobs` passes `compact -L N` (`-L/--fold-tasks`; free among `compact`'s `b c g A D P S` and `compact_options`' `C F G j K m M p r T`). It needs `-S`.

1. **Fold VM: plan + runs' spill** (`_plan`, `_runs_spill`, `_short_plan`). As today (steps 1–2). The `ChunkSpill` partitions are saved as step `fold/fan/runs` (`in/<run>/sp=<p>/…`, doc: parts, chunks, bytes; the bytes logged, since c001 didn't), the members as `fold/fan/members` (`dmem`, `mtid`, the fold VM's own), then the plan as `fold/fan/plan` (marker last): tables `tup`, `tch`, `fch` (each chunk's tid/`c0` span and estimated rows), `fbase` (the base's long roots files: generation-relative path, size, `q` span), `fshort` (every tier's short roots files likewise), `sqg` (the short bounds with estimated rows); doc: tiers, chunks, short ranges, `source`, `chunk_rows`. It skips staging long/short roots on its own disk: each task (share 0 too) stages its own share's files.
2. **Wave 1 (`DigestClasses` only).** One helper job. Its units are every long chunk's `digest` unit → `fold/folded/digest/c<ch>/{dig.parquet,done.json}`, plus every short range's spill (it doesn't need `al`, so it fills the wave). The fold VM gathers the digests into `tdig`, then computes `al`, `ctid` and the long `qg` (as `lqg`) with the existing SQL (`_classes`). It saves them as step `fold/classes` (`al`, `ctid`, `lqg`, `tdig`, `dgq`; doc: the long range count, `classes: digests | roots`, `fixed`, `respill`), which the wave-2 helpers load. `fold/fan/members` carries `mt` too, so a resumed fold can derive `tdig` again.
3. **Wave 2.** One helper job: every long chunk's `long` unit: the chunk combined again from its inputs (decision 1: no `troots` between waves; the fold VM logs the rows combined again), joined to `ctid`, spilled by `lqg`. With `PersistedClasses` there is no wave 1, so wave 2 also carries the short ranges and is the only wave (a resumed fold deals each wave only its units without a `done.json`, so the short units land wherever they weren't done).
4. **`spilled`.** Once every unit is done, the fold VM writes `fold/spilled-tables` (`al`) and `fold/spilled/done.json` (`Checkpoint.mark_files`). Its `files` list is the sorted union of the units' `done.json` file lists, so the marker has the `save_files` shape without the fold VM uploading anything. The 730 GB upload goes away from the critical path: each task uploads its own ~1/N. `fold/fan/runs` is dropped with the long roots' steps once `spilled` is saved.

Deal: chunks are dealt **contiguously** (`static_fanout.contiguous`: in unit order, each unit to the share its estimated rows' midpoint falls in, so a share is within one unit of the mean, counting the heavy single-tuple chunks; each share's estimated rows are logged with max/mean), not by `static_merge.deal`'s LPT. Contiguity keeps a share's base reads to ~1/N of the base's long roots (the chunks are `c0`-ordered and the base files are `q`-sorted) and its runs partitions to ~64/N. Short ranges are dealt contiguously too. Re-deals of missing units (`Helpers.wait`) stay scattered: a re-dealt chunk stages its own files. Share 0 runs on the fold VM through the existing `_pool` (`jobs`), and its outputs are uploaded like a helper's.

### What a helper reads

| input | from | per helper (N = 16) |
|---|---|---|
| plan tables (`tup`, `tch`, `fch`, `fbase`, `fshort`, `sqg`; wave 2 adds `al`, `ctid`, `lqg`) | `fold/fan/plan/`, `fold/classes/` | MBs |
| base long roots files overlapping the share's `c0` span | the generation's store URL (`source`), `blobstore.stage_files` | ~1/16 of the base's long roots (base drill 279 GiB long + short) |
| runs spill partitions overlapping the share's chunks (fetched, then split by chunk as `ChunkSpill` does) | `fold/fan/runs/files/` | ~4 of 64 partitions |
| base + runs short roots overlapping the share's short ranges (wave 1) | `source` | ~1/16 of the short roots |

Helpers stage, never read through a gcsfuse mount. Their jobs stay `mount=False` (`static_merge.py:451`): in-place gcsfuse reads ran 3–4× amplified (`static_compact.py` staging note above `STAGE_MODES`). The generation is published and immutable, so staging from `source` is safe. The fold VM never uploads base inputs.

### What comes back, and where

- **Wave 1 digests:** `fold/folded/digest/c<ch>/dig.parquet` (KBs), fetched by the fold VM.
- **Spills:** written straight to their measure-ready place, `fold/spilled/files/<kind>/<producer>/grp=<g>/*.parquet`. `<producer>` = `c<ch>.<attempt>` (long) or `s<g>.<attempt>` (short); `<attempt>` is a per-task nonce. Each unit's `done.json` (`fold/folded/<kind>/<unit>/done.json`) lists exactly its files and stats, and is written last.
- **No round trip through the fold VM.** Measure helpers (`measure-shard`) already fetch from `spilled/files/`. `_spilled` reads `spilled/done.json`'s file list (sizes from one listing; a listed file missing raises) instead of listing the prefix, so an aborted attempt's orphan files are never read. The fold VM keeps its own fold share's spills and fetches only the rest of measure share 0's; with `measure_tasks` 1 it fetches every spill it lacks (`load_files(missing_only=True)`).
- **Lazy `spilled` restore (built, its own commit; fixes today's measure fan-out after a resume).** `fold_drill` restored the whole step (730 GB) even with the measurement fanned out. With a measure fan-out it now restores only `spilled-tables` + the marker's doc; `Fanout.start` deals by the marker's files and listed sizes (`spill_sizes(work, …)` is gone) and fetches only share 0's spills missing from the work disk; `measure_ranges` keeps the ranges dealt away without local spills.
- **Short: fold + measure fused (optional, phase 2).** Short unit `g` is exactly measure range `g`, so a helper could measure it in the same task and upload `measured/short/g<g>/…` directly. That skips the short spills' upload and download, and lets short measurement finish inside wave 1, overlapping wave 2.

## Failure, retry, resume

- **Reuse `static_fanout.Helpers`.** `FoldFanout(Helpers)` with `root = "folded"`. It implements `unit_key(kind, i)` → `folded/<kind>/c<ch>|s<g>` and `wave(n, weights, here)` (deal, submit, share 0 via `here`, wait, join); the class source gathers the digests after wave 1. Same plan files (`folded/plan-<n>.json`: wave, tasks, code; `source` and `chunk_rows` are in `fold/fan/plan`'s doc), submit thread, `wait` / re-deal (≤ 2 re-deals per wave: `Helpers.base` counts the jobs before the wave; spot `maxRetryCount` 3), and failure naming the units (`fold fan-out: units ['long/c000012', …] not folded after 3 helper jobs`).
- **Idempotent units.** Skip if `done.json` exists. Write under a fresh `<attempt>` producer, so a partial or reclaimed attempt's files are orphans the marker never names. `clear_checkpoints` removes them with the rest.
- **Per-task work dir.** `<tmp>/<plan>-<i>`, cleared first, as `measure-shard` does (`static_merge.py:904`), since Batch reuses VMs.
- **Code-keyed.** `-V <code>`: a helper on other code refuses. Helpers run the profile's image (pinned by digest).
- **Resume.** A rerun after a reclaim restores `fold/classes` if saved (skips wave 1). It deals only units without `done.json`, and rebuilds the `spilled` marker once all are done.
- **`seed_checkpoint`.** `STEPS` gains `fold/fan/runs`, `fold/fan/members`, `fold/fan/plan`, `fold/classes` after `fold/long-roots` (before `fold/spilled-tables`). When `fold/fan/plan` is copied and `fold/spilled` isn't, the finished `folded/` units are copied too, each with the spill files its `done.json` lists (`helper_units(…, listed)`); without the plan none are (their chunk numbers are its).
- **Lease.** Unchanged: the fold VM holds and renews it while it waits. Heartbeat stages: `drill/fold/wave <n>: plan`, `…: <k> units here`, `…: waiting for <k> helper units`.

## Machine, quota, profile

- **Stage.** `fold-shard`: `machines["fold-shard"]` and `ssd_gbs["fold-shard"]`, spot with retries when the profile is. Built by `fold_helper_job(runner, …)` through the real `runner.job("fold-shard", scan, n, "static_merge", ["fold-shard", "-c", url, "-P", plan, "-V", code, "-j", fold_jobs(machine), *duckdb_args(machine)], mount=False)` and `_helper_recorded`, like `helper_job` / `anchor_helper_job` (`static_merge.py:445`). It is named and labelled by the compaction's newest scan.
- **Shape.** n2-highmem-32 + 1,500 GB local SSD. Per task: ~15–30 GiB of staged inputs, DuckDB temp for `-j` chunks at once, and ~46 GB of spills written. Spills are uploaded per unit and then deleted, so the steady state is far below the single VM's 1,019 GiB peak.
- **Quota (80 TB N2, us-east1).** 16 × 1.5 TB fold + 3 TB fold VM = 27 TB. If short measurement overlaps wave 2 (phase 2), add up to 16 × 1.5 TB of `measure`, about 51 TB, still under the quota. The appends' own jobs share it, so check before raising N. If Batch can't run all tasks at once, it runs them in turn (correct, just slower). That is why the waves are separate jobs, not an in-task barrier: tasks blocking on a barrier while their peers wait for quota would deadlock.

## Expected wall time (N = 16, today's `DigestClasses`)

Compute in core-seconds from c001 (48 vCPU): long-roots ≈ 147 K, canonical ≈ 99 K, short ≈ 71 K. Wave 1 ≈ 0.8 × long-roots (no `troots` write) + short ≈ 188 K. Wave 2 ≈ 0.8 × long-roots + canonical ≈ 216 K. That is ~10 % more than today, because the combine runs twice. Over 15 × 32 + 48 = 528 vCPU at ~75 % efficiency, each wave is ~8–9 min, plus ~4 min Batch provisioning and ~1–2 min staging/upload.

| | today | fanned out |
|---|---|---|
| plan + runs' spill + runs upload (fold VM) | 223 s | ~5 min |
| wave 1 (digests + short) | 4,525 s (long-roots + short) | ~14 min |
| classes | 7 s | <1 min |
| wave 2 (canonical spills) | 2,054 s | ~15 min |
| `spilled` upload | 1,463 s | 0 (in the waves) |
| **fold** | **~2.6 h** | **~35 min** |

With `PersistedClasses`: one wave, ~20 min. Stragglers: one tuple is one chunk, so a 440M-root class is one chunk (~54× the 8.2M-row mean). At c001's ~5 s per mean chunk that is ~5 min alone, under a wave. Report per-task seconds (`folded/tasks/<plan>-<i>.json`) as the measurement does.

End to end, that puts a from-scratch compaction at about 37 min intervals/shards/catalog + 35 fold + 40 measure + 30 anchors ≈ 2.4 h before publish. ≲ 2 h needs also overlapping short measurement with wave 2 (phase 2) and/or `PersistedClasses`, or the shards/catalog levers.

## Invariants and tests

Tests (`test_static_compact.py`, world C: chunks of 3 roots, so a class's tuples span chunks and a range's spills span producers; in-process helpers via a fake submitter calling `fold-shard`'s body):

- **Same bytes.** `test_a_fanned_out_fold_writes_the_single_vm_fold` (`fold_tasks` 3 vs the single-VM `g.c001`, md5 for md5); `test_every_fan_out_at_once_writes_the_single_vm_fold` (fold, measure and anchors each over 3 tasks: the whole compaction). Lazy restore: `test_a_fanned_out_measurement_resumed_after_the_spills_fetches_only_its_share` (measure 3 after a resume = measure 1's bytes, exactly share 0's spill files fetched, no full restore).
- **Class sources agree.** `test_a_fanned_out_fold_writes_the_single_vm_fold[digests|roots]` (the runs' `digests: delta` marks removed for `roots`): same bytes, waves exactly as above; `test_a_wrong_change_is_corrected_from_the_roots_over_the_fan_out` (a run's change off by one root: `fixed`, wave 3 iff the classes changed, same bytes).
- **Re-deal / failure.** `test_fold_units_a_helper_job_left_are_dealt_again[1|2]`, `test_fold_units_never_folded_fail_the_fold_by_name`, `test_an_orphaned_spill_is_never_read` (a spill under `spilled/files/` no `done.json` lists).
- **Resume / seed.** `test_a_fold_stopped_after_its_classes_skips_the_digest_wave`, `test_a_fold_stopped_mid_wave_two_deals_only_its_missing_units`, `test_a_seed_through_the_fold_plan_carries_the_finished_fold_units_and_their_spills`.
- **Real job spec** (`test_compact_stage.py`). `test_the_fold_fan_outs_helper_job_is_a_real_stage_job_named_by_the_compactions_scan`: `fold_helper_job` via the real `runner.job`: name, stage label, task count, machine and SSD from the profile, spot/retries, the record env, and the args parsed back through `fold-shard`'s click command. `test_a_fold_fanned_out_compaction_gets_its_task_count_and_the_profile`: `compact_jobs` passes `-L N` (with `-D`, `-A` too), parsed back by `compact`; the profile env round-trips; `STATIC_NAMES_FOLD_TASKS` loads.
- **Work dir.** Each fake task's dir is pre-seeded with a stale file, which must be gone.
- `test_contiguous_shares_keep_order_and_balance_around_a_heavy_unit`.
- Not on the laptop: CI / dev node.

## Decisions (code owner's review)

1. **Re-combine, don't ship `troots`.** Wave 2 re-reads and re-combines each chunk's inputs; the fold VM logs the rows combined again.
2. **Class source pluggable.** The seam produces `tdig` (`derive(con, tiers, log)`; false → the digest wave); `al` and the long ranges derive from it either way. `DigestClasses` was built first; with step 3 on `cloud`, `PersistedClasses` is wired to the real `_tuple_digests` and is the default, falling back to the digest wave.
3. **Runs' spill stays on the fold VM** for v1; its bytes are logged (`drill/fold/runs-spill … bytes`, and `fold/fan/runs`' doc).
4. **The fold VM keeps share 0.**
5. **Phase 2 (not now):** fold + measure of short ranges in the same task; overlapping the measurement with wave 2.
6. **Separate Batch jobs per wave**, no in-task barrier.

7. **Heavy chunks: no memory-class routing in the deal.** A single-VM fold at `-M 200GB -p 48` OOM'd combining c001's heavy tail (chunk 4875/4881, the `ct` combine at 186 GiB of 186 GiB; it passed at `-M 270GB`, RSS peak 322 GiB). The answer is the root session's `hash(path) % P` split of a heavy chunk's combine (P from the plan's per-chunk estimate; groups disjoint across parts, the same bytes), on `cloud` as 2261056c (`COMBINE_PART_ROWS` 64M). It lives in `_combine`, so the fan-out's units (`fold_units`) split exactly as `_long_roots` does (their part files under the task's `split/`, removed after each unit; `test_heavy_chunks_combine_in_parts_over_the_fold_fan_out`). The deal stays purely contiguous by estimated rows.

## Open questions

1. **N and shape.** Pick them from the heaviest chunk and the quota left beside measure/anchors helpers and the dailies.
2. **First-run measurements.** The re-combine's real cost (decision 1), the runs' spill bytes at higher compaction levels (decision 3), and share 0's upload on the fold VM (decision 4).

[static-measure-fanout.md]: static-measure-fanout.md
[static-anchors-fanout.md]: static-anchors-fanout.md
[static-incremental-compaction.md]: static-incremental-compaction.md
[static-compaction-perf.md]: static-compaction-perf.md
