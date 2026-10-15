# The run drill under spot preemption

**Status:** Part 1 (checkpoint / resume) built. Part 2 (splitting the long kind across tasks) designed below, not built.

## Problem

A static name index run's drill (`static_drill build -k task`, one Batch job of two tasks: long ∥ short, `static_runner.Runner.drill`) is a long single task per kind. On spot, Batch retries a reclaimed task (`maxRetryCount: 3`) with the same args, and the retry started from zero: up to the task's whole length lost per preemption (gcs's long task: ~50 min before the 10-14 settling fix, ~29 min estimated after). The alternative so far was listing `drill` in the profile's `standard_stages` (no spot discount).

## Part 1: checkpoint / resume (built)

### Mechanism

`dt_cloud.checkpoint` (generic, not drill-specific):

- `Checkpoint(root, prefix, every_s=, work=)`: a `blobstore.Store` root (a dir, or `gs://` / `r2://` / `s3://`) and a key prefix. `sub(name)` nests (`prefix/name/`).
- **Steps** — `save(con, step, tables, doc)` / `done(step)` / `load(con, step)`: DuckDB tables as parquet (staged through `work`), then `<step>/done.json` last (the completeness marker: column names + DuckDB types, and the doc). A load recreates each table with its recorded types and casts into it; `HUGEINT`/`UHUGEINT` columns ride as text (DuckDB's parquet writer turns `HUGEINT` into a double, which would corrupt the digests).
- **Parts** — `Parts(ck, step, schema)`: a loop's output so far. `add(rows, done)` after each item; every `every_s` (default `PART_INTERVAL_S` = 300 s) it writes `part-NNNNN.parquet` (the rows since the last part) then `progress.json` (`{done, parts}`). A rerun's `Parts` exposes `start` (items covered) and `loaded` (their rows, in order); the loop skips the first `start` items. `flush(done)` at the loop's end (nothing when no item was done since the last part).
- **Files** — `save_files` / `load_files`: a finished sub-job's output files (used when a build does both kinds in one process).
- `clear()` deletes everything under the prefix.

A reclaimed task loses at most: the step it was in (for the one-shot steps), or `every_s` of a loop.

### The drill's steps

`build -S <root>` keys the task's checkpoint `<root>/drill/<gen>/<scan id>/<kinds>/` (`drill_checkpoint`: `<kinds>` is `long` or `short` for the two Batch tasks, `long-short` for `-k both`), so the two tasks never share or clear each other's. Under it, per kind (`<kind>/`):

| step | where | holds | skipped on resume |
|---|---|---|---|
| `inputs` (task level) | `build_cmd` | `pnew` (new paths), `nmh` (the new members' history), doc `{new_members}` | the `pnew` query, `_new_members`, `history_table` |
| `long/digests` | `long_day_roots` | `dg` (per member, per chunk digest partials) | the digest member loop over every suffix chunk |
| `<kind>/roots` | `build_day` | `dr`, `newq`, long: `cls`; doc = the kind's `classes` / `new_members_list` or `short_delta` | the delta-roots member loop (long), `short_roots` (short) |
| `<kind>/probes` | `day_rollups` → `_probe` | `pr` | the probes' tier reads |
| `<kind>/settle` (parts) | `_settle` | settling's rollup rows (`ROLLUP_ARROW`) per finished directory prefix of `aff` (ordered `q, dir`) | the settled directories |
| `<kind>/counts` | `day_rollups` → `_base_counts` | `bx` | the base counts below the measurement's floor |
| `<kind>/heavy` (parts) | `newly_heavy_rollups` | the newly heavy directories' full rollup rows, per finished prefix of `newh` (ordered `q, dir`) | the built directories |
| `long/written` | `build_day` | the long kind's output files + its meta part | only when another kind follows in the same process (`-k both`, `static_local`); a Batch task's last kind is followed by the upload instead |

What is **not** saved, and reruns on resume: the SQL between the steps (`_cmap`, `tdir`, `stf`/`hdr`/`kc`/`rc`, `tch`/`nk`, `nkc`/`kt`/`cand`/`aff`/`joins`, the unaffected `rup` rows, `dc`/`nh`/`newh`, `dcnt`), `meas`, and settling's prologue (`_settle_ctx`, `_heavy_totals`, `_settle_plan`, restricted to the remaining directories). These are deterministic functions of the restored tables and the read-only inputs (the prior tiers' files), measured at "a minute or two" each on gcs (specs/architecture/static-name-search.md, "Runs: settling's cost"); saving them instead would mean shipping GBs of intermediates (`stf`, `tch`) per checkpoint.

Why it is byte-identical: every resumed loop's per-item output depends on that item alone (a settled directory on its own kept/others/candidates/ctx and the tiers' reads; a newly heavy directory on its own prior roots and today's), the restored tables are the saved ones with their exact types, and the output files are written from `dr` / `rup` / `dcnt` / `cls` with a total `ORDER BY` (`write_kind`, `write_state`, `write_aliases`), so row order in the tables never reaches the bytes. `meta.json` is the same but for its times (`s`) and the tiers' read counters (`reads`, which count this process's reads) — as between any two builds.

The checkpoint is cleared after the upload (`build_cmd`), and also by a rerun task that finds its kind already uploaded (reclaimed between the upload and the clear). A run that never finishes leaves its checkpoint; put it in a bucket with an age-based delete rule (the scratch bucket has one).

### Enabling it

The profile's `drill_checkpoint` (env `STATIC_NAMES_DRILL_CHECKPOINT`): a store root, a bare bucket name meaning `gs://<name>` (`blobstore.store_url`). `Runner.drill` then passes `-S <it>` to both tasks (the backfill's drill builds too). No default anywhere in code; e.g. gcs would set it to its scratch bucket (`gs://<scratch>` — the stages' service account writes there already). With it set, `drill` can leave `standard_stages` and run spot.

`static_drill build -S/--checkpoint` (env `STATIC_NAMES_DRILL_CHECKPOINT` too) for a manual run. `static_local`'s single-machine `add` passes none (not spot).

### Tests (`cloud/tests/test_static_drill.py`)

- `test_an_interrupted_build_resumes_to_the_same_bytes[run1..4, both configs]`: a checkpointed build (`every_s=0`: a part per item) writes the uncheckpointed build's files byte for byte; its saves are pinned exactly (`SAVES`: each step, and each loop's part count). Then, for each step and each loop's first / middle / last part: a build interrupted right after that save (an exception from `on_save`; its connection's new tables dropped and its output dir deleted, as a reclaimed task's), then a rerun on the same checkpoint: the rerun's saves are exactly the ones after the interruption (it resumed, nothing recomputed that was saved), its files equal the uninterrupted build's, and `clear()` leaves nothing.
- `test_a_checkpoint_restores_tables_exactly`: types (a `HUGEINT` beyond a double's precision, `UTINYINT`, nulls, booleans), rows and doc of a step; a part series' rows in order and `start`; the exact key layout; `clear`.
- `test_the_build_inputs_checkpoint_round_trips`: `save_inputs`' `pnew`, new members and history (schema and rows).
- `test_static_runner.py::test_the_drill_tasks_checkpoint_where_the_profile_says`: `drill_checkpoint` (and its env var) adds `-S` to the drill job.

Before the change the first fails at once (`build_day` takes no `checkpoint`). Mutations it catches: a settle resume that drops the saved parts' rows (files differ), and a checkpoint that never reports a step done (the rerun redoes every save).

## Part 2: splitting the long kind across N tasks (design, not built)

### Why not by class / key range

The obvious split — partition the long kind's classes into N `q` ranges, each task running the whole kind on its range, a combine writing the union — is correct (classes only split, so partitioning by the *previous* canonical keeps each class in one task; every downstream table, `cmap`, `tdir`, `stf`, `hdr`, settling, `newh`, `dcnt`, is keyed by the class canonical; `write_kind` sorts and cuts files by global `q` counts, so the combine's files are the single task's). But its floor is the largest class: gcs's long delta is concentrated in a few huge members ("a few huge members hold most of both", static-name-search.md), so one task would get most of the work. Each task would also rescan every suffix chunk for its digests and delta roots (the member loop's scan is per chunk, not per member), and recompute `pnew`.

### Recommended: fan out the per-directory loops

The long task's critical path after the roots is per-directory work: settling (~10 min estimated post-fix) and the newly heavy directories (765 in 402 s on 10-14), plus the probes and base counts. Those loops are exactly the ones Part 1 made resumable from an item index, because each item's output depends on that item alone. So split them by item, not by class:

1. **`build --phase prepare`** (1 task per kind, as now): inputs, roots, the SQL, probes, base counts — the Part 1 checkpoint through `counts` — and stops, the checkpoint holding everything the loops need (no new state: the loops' inputs are recomputed from `dr`/`cls`/`newq`/`pr`/`bx` by the same SQL).
2. **`build --phase loops -i I -n N`** (N tasks per kind, `BATCH_TASK_INDEX` = I): restores the `prepare` checkpoint, reruns the SQL to `aff` / `newh`, then settles `aff[I::N]` and builds `newh[I::N]` (strided, since the costly directories cluster in a few members' `q` ranges), saving each as `Parts` under `<kind>/settle.I-of-N` / `<kind>/heavy.I-of-N` (resumable as now).
3. **`build --phase write`** (1 task per kind): restores `prepare`, reruns the SQL (`rup`'s unaffected rows, `dcnt`), appends every slice's parts to `rup`, writes, uploads, clears.

Byte-identical to one task: `rup` is the same multiset of rows (each directory's rows are its own, whichever task settled it), and the files are written with a total order. The restated count is recomputed from the parts. Test: for N ∈ {1, 2, 3}, `prepare` + N `loops` + `write` over the fixture's runs equals `build_day`'s files (`_tree`), and an interruption inside a slice resumes that slice only.

Runner: the drill stage becomes three jobs per run (prepare: 2 tasks; loops: 2·N tasks, long's N from the profile, short's maybe 1; write: 2 tasks), each skipped when its checkpoint marker / output exists. Cost: the SQL prefix (`_explode` over `dr`, `stf` from the tiers' rollups) is paid 2 + N times instead of once — a minute or two each — so N is worth it only while the loops dominate.

### Before building it

Measure on a real run, from the existing log lines, what the long task spends in roots (digests + delta roots chunks), the SQL, probes, settling, base counts, newly heavy, and write. If the roots' member loops dominate (they are not split by this design), the lever is instead chunk-level parallelism there (the chunks' `dg`/`dr` partials are additive: N tasks over disjoint chunk ranges, combined by `sum` / union), which can reuse the same `Parts` layout.

## `sn-merge` (the binary counter's carry, one 29-min spot task)

Same exposure, and the same mechanism applies, at tier granularity: `static_merge.build_merged_run` builds `shards`, `catalog`, `drill`, `anchors` (in spawned processes with `-j`), each writing a whole output dir. A `Checkpoint` keyed `merge/<gen>/<merged run key>/` would `save_files` each tier's dir as its process finishes and `load_files` the finished ones on a rerun (skipping those tiers), clearing after `publish_revision`. Finer resumption inside a tier (e.g. `merge_drills`' per-kind `merge_tiers`, or the catalog merge) would need that tier's own steps, as here. Not built.
