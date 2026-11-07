# Static compaction: the base anchors fanned out over Batch tasks

## Why

gcs's compaction spends over an hour in base anchors on one VM (the c001 race: 18 cores, 180 GiB RSS for 51+ min). The rollups are already independent per `(kind, shard)` task: `static_anchors._tier_parallel` runs `_shard_task(kind, [file], …)`, each writing one sorted cells parquet, streamed in shard order by `write_rollup_parts` / `write_start_parts`. The kinds are `end` (over the suffix shards), `exact` and `start` (both over the name shards). So the tasks can be spread over helper Batch tasks, as the drill measurement already is ([static-measure-fanout.md]).

## Shape

Opt-in per profile: `anchor_tasks: N` (`$STATIC_NAMES_ANCHOR_TASKS`; default 1 = today's path, unchanged). `compact_jobs` passes `compact -A N` and the profile inline in `$STATIC_NAMES_PROFILE`, as for `measure_tasks`.

1. **Fold VM:** builds `names/` as today (`static_local.build_base_anchors`), then `static_fanout.AnchorFanout.start`:
   - deals the `(kind, shard index)` tasks by input bytes over N shares (`static_merge.deal`), keeping share 0;
   - uploads the inputs of the other shares' tasks not done yet under the compaction's checkpoint, `anchors-in/sx/s<i>.parquet` (suffix shards) and `anchors-in/names/s<i>.parquet` (name shards, read by `exact` and `start`), each once, skipping one already there at its size (a resumed compaction's shards are the same bytes);
   - writes `anchored/plan-<n>.json` (tasks, code version, `R`, `K`, `hex_runs`) and submits a helper job of N−1 tasks on a thread.
2. **Share 0:** runs through the existing `_tier_parallel` pool (`ConPool`), into the same `work/<kind>-<i>.parquet` files.
3. **Helpers:** `static_merge anchor-shard -c <ckpt url> -P <plan> -V <code>`, stage `anchor-shard` (its own: `anchors` is a long single-task STANDARD stage; the helpers are retryable, so spot with retries when the profile is; `machines["anchor-shard"]` sizes them). Built through the static runner's own `job()` (`anchor_helper_job`), named and labelled by the compaction's newest scan, never the plan's key. Each task:
   - refuses a code version other than the fold's;
   - works in a dir of its own per plan and task, cleared first;
   - skips a task whose `anchored/<kind>/s<i>/done.json` exists; else fetches its input, runs `_shard_task` (`ANCHOR_THREADS` threads each, as many at once as `-p` allows), uploads `cells.parquet`, then `done.json` (`doc`, `s`, `rows`);
   - ends with `anchored/tasks/<plan>-<i>.json`.
4. **Collect:** `AnchorFanout.collect` waits for every away task's `done.json`, re-dealing the missing ones to a new helper job when one ends without them (at most 2 retries), then fails naming them (`anchors fan-out: shard tasks ['end/s00003', …] not built after 3 helper jobs`). It downloads each cells file to the path `_tier_parallel` would have written, and the existing `write_rollup_parts` / `write_index` / `meta.json` / `write_start_parts` path continues.

Shared with the measurement's fan-out (`static_fanout.Helpers`): the deal, plan write, submit thread, wait / re-deal and the `done` check. `Fanout` (measure) keeps its keys, messages and behaviour.

Liveness: heartbeat stages `anchors/fan-out plan`, `anchors/fan-out inputs: …`, `anchors/rollups (share 0)` (the progress line), `anchors/rollups: waiting on the helpers' N shard tasks`; one log line when the helpers are submitted.

## Invariants

- **Same bytes:** the published generation equals the single-VM fold's. With a fan-out, `build_tier_local` always takes the shard-by-shard path, which `test_parallel_tail_is_the_serial_one` already holds equal to the serial one.
- **Lease and checkpoint:** as the measurement's: one process holds the compacting lease throughout; `anchors-in/` and `anchored/` live under the compaction's code-keyed checkpoint until `clear_checkpoints`.

## Status (implemented)

- `static_fanout.Helpers`, `Fanout` (refactored onto it), `AnchorFanout`; `static_anchors.shard_tasks`, `_tier_parallel(fanout=…)`, `build_tier_local(fanout=…)`; `static_local.build_base_anchors(fanout=…)`; `static_compact.fold_gen(anchor_fanout=…)` (and `fold`, `compactor`).
- `static_merge`: `anchor_fanout`, `anchor_helper_job`, `ANCHOR_STAGE`, `anchor-shard`; `compact -A N`, `compact-probe -A N`.
- `compact`'s measure flag is now `-D` (`--measure-tasks`): `-F` collided with `compact_options`' `-F/--read-from`, so a `measure_tasks` profile's `compact -F 3` was refused by click (`'3' is not one of 'gcs', 'r2'`). `compact-probe` keeps `-F` (no clash there).
- Tests: `test_fanned_out_anchors_write_the_single_vm_fold` (3 tasks vs `g.c001` at 1, in-process helpers), `test_anchor_tasks_a_helper_job_left_are_dealt_again`, `test_anchor_tasks_never_built_fail_the_fold_by_name` (`test_static_compact.py`); `test_the_anchors_fan_outs_helper_job_is_a_real_stage_job_named_by_the_compactions_scan`, `test_an_anchors_fanned_out_compaction_gets_its_task_count_and_the_profile` (and the measure flag parsed back by `compact`; `test_compact_stage.py`).

## Open

- **N and the machine:** inputs are dealt by bytes; a heavy name shard (its `exact` and `start` tasks) can still dominate one helper. Pick N from the shard sizes, and size `machines["anchor-shard"]` / `ssd_gbs` under the SSD quota as for `measure`.
- **The fold VM's share:** it also uploads every away shard first (one pass of the suffix and name shards' share N−1/N); measure it on the first run.

[static-measure-fanout.md]: static-measure-fanout.md
