# Static compaction: the drill measurement fanned out over Batch tasks

## Why

gcs's compaction `2026-10-08c → c001` (n2-highmem-48, `-M 270GB -p 48 -j 8`) spends ~6 h of its ~10 h in `drill/measure` (`static_local.measure_ranges`): 5,371 long + ~800 short `q` ranges, each read from the fold's spills, measured, rolled up and written sorted. Two separate costs:

- **Every range is independent:** a range's outputs (its sorted roots, its measured dirs and rollup rows) depend on its spill files alone, so N machines can measure the ranges in ~1/N of the time. `measure-probe -S I/N` (`static_merge`) already does this read-only over a saved checkpoint, and `measure-probe-merge` checks the union is the unsharded run's bytes.
- **The pooled measurement spills:** at ~20 GB of `memory_limit` per process, the stage disks wrote about what they read (~1 GB/s over 8 disks). `measure_report` and the sampled `measure-probe -H/-s` runs tell which ranges and which step. That diagnosis comes first; the fan-out shouldn't hide it.

## Shape

The compaction stays **one job**: the fold VM keeps the lease and its local state, and adds helpers for the measurement. It's opt-in per profile: `measure_tasks: N` (default 1: today's job, unchanged).

1. **Up to `spilled`:** the fold VM folds as today, up to `spilled` (saved to the checkpoint as today, so the helpers can fetch from it).
2. **Helpers:** with `measure_tasks > 1`, the fold VM submits a helper Batch job of N−1 tasks: `static_merge measure-shard -c <fold ckpt> -S <I>/<N> -o <fold ckpt>/measured/`, I = 1 … N−1.
   - **Image:** submitted by the fold job's own image digest, not a tag. `$DT_IMAGE_DIGEST`, which the runner sets when it submits the compaction, resolved once. `:latest` moving mid-compaction would change `code.json`, and the code-keyed checkpoint would then be cleared.
   - **Spot:** the helpers run on spot with `maxRetryCount` ≥ 3, since skip-if-done makes them safely retryable. The fold VM stays standard.
   - **Permissions:** the job's service account needs `batch.jobs.create`. The head already submits; check that the compaction's SA does too, and add it in the deployment's IaC if not.
3. **Share 0 on the fold VM:** the fold VM measures share 0 itself from its local spills (no fetch). Its outputs go straight into the tables and `work/<kind>-sorted/`, as today.
4. **Each helper task:**
   - fetches only its ranges' spills (`_fetch_ranges`, `deal`) and measures them (`measure_ranges`);
   - per range `g`, uploads `measured/<kind>/g<g>/{sorted,dirs,rup}.parquet`, then `measured/<kind>/g<g>/done.json` (seconds, rows, files, temp bytes);
   - skips a range whose `done.json` exists, so a reclaimed task resumes;
   - after its last range, writes `measured/tasks/<I>.json`: the task's seconds and ranges.
5. **The fold VM waits, renewing the compacting lease meanwhile:**
   - It waits until every range has a `done.json`, or the helper job ends.
   - If the helper job ends with ranges missing (a task out of retries), it resubmits a helper job for just the missing ranges, at most twice, then fails naming them.
   - It then downloads only the helpers' outputs. That's sorted roots of ~(N−1)/N of the spills' size, not the ~900 GB a fresh VM's restore of every part would cost.
   - It loads every range's `dirs` / `rup` into the tables in `q`-range order, and continues `finish_drill` (roots-measure, `build_drill`), anchors and publish, as today.
6. **Resume:** a fold VM rerun after a reclaim resumes at `spilled` as today, keeps every helper range already done, and measures or reassigns the rest.

## Invariants

- **Same bytes:** the published generation is byte-identical to the single-VM fold. The test is `test_compacted_generation_is_the_rebuild` at `measure_tasks` 1 and 3 over the fixture, with the helpers run in-process by a fake submitter.
- **Lease:** one process holds the compacting lease for the whole compaction, renewing it while it waits. A daily's `add` never sees a gap.
- **Checkpoint lifetime:** `spilled` and `measured/` stay until the compaction is published (`clear_checkpoints`, unchanged).
- **Code-keyed:** helpers write under the fold's checkpoint, keyed by the same code (one image digest). A rebuilt image clears and redoes them, as any step.

## Stragglers

`deal` balances by spilled bytes, but one heavy `q` range can't be split. Pick N so the heaviest range is under ~1/N of the total; the sampled `-H 5` probe gives the heaviest. `tasks/<I>.json` reports each task's seconds, so a straggler shows up.

## Status (implemented)

- **`static_fanout.Fanout`:** the fold VM's side.
  - **`start`:** deals both kinds' ranges by spilled bytes over `measure_tasks` shares (`static_merge.deal`), keeps share 0, writes `measured/plan-<n>.json` (shares, code version, `R`/`K`/`floor`) under the fold's checkpoint, and runs the helper job on a thread.
  - **`collect`:** waits for each range's `done.json`, re-deals the missing ones to a new helper job when one ends without them (at most `retries`, default 2), then fails naming them. It downloads each range's `sorted`/`dirs`/`rup` where the local measurement's would be.
- **`static_local.measure_ranges(remote=…, on_range=…)`:** skips the remote ranges, then collects them, so the tables and files (the drill's bytes) are a single VM's. `finish_drill(fanout=…)` starts and finishes the fan-out around both kinds.
- **`static_merge measure-shard`:** the helper.
  - It refuses code other than the fold's (`-V`).
  - It skips ranges already done, fetches only its ranges' spills, measures them, and uploads each range's files, then its `done.json`.
  - It ends with `measured/tasks/<plan>-<i>.json`.
- **`static_merge compact -D N`** (was `-F`, which collided with `compact_options`' `-F/--read-from`: click took `-F 3` as a read source and refused it): `measure_fanout` builds the helper jobs with the runner's own `job()` (stage `measure`: the profile's machine for it, spot with retries when the profile is) from the profile, which the runner passes inline in `$STATIC_NAMES_PROFILE` (`to_mapping`; `load_profile` takes JSON). Profile field `measure_tasks` (`$STATIC_NAMES_MEASURE_TASKS`) makes `compact_jobs` pass `-D`.
- **Lease:** held by the compaction's own heartbeat while it waits; nothing changes there.
- **Image:** helpers run the profile's `image`, as every stage job does. A deployment pins it by digest; a moved tag would surface as `measure-shard` refusing a different code version, not as different bytes.
- **Tests:**
  - 3 tasks write the single-VM fold's bytes;
  - a helper task that never runs has its ranges re-dealt, same bytes;
  - helpers that never measure fail the fold naming the ranges;
  - the compaction job carries `-D` (parsed back by `compact` itself) and the profile, which loads back as the runner's.

## Open

- **Local SSD quota:** `LOCAL_SSD_TOTAL_GB_PER_VM_FAMILY` (gcs: 25,000 GB of N2 in us-east1) caps how many helper VMs come up at once. gcs's sharded probe got 4 of 16 n2-highmem-32s (1.5 TB each) beside its other jobs, and Batch ran the rest in turn on them. Size N × the `measure` stage's SSD (`machines` / `ssd_gbs` `measure` in the profile) under the quota, together with whatever else runs: an n2-highmem-16 at 750 GB fits twice as many. Raising the quota is a deployment decision. `measure-shard` works in a dir of its own per plan and task, cleared first, since a reused VM's earlier task leaves its files.
- **Helper machine and N:** they come from the heaviest range. With ef95df4d (heavy ranges first, wide), one helper task may still take a 440M-root range alone, ~500–1,000 s.
