# Handoff: drilldown runs, their place in `runs add`, and compaction

Written 2026-10-09 by the session that built the drill runs and started compaction, for the agent that continues. Specs to read first: `specs/static-append.md` ("Drilldown runs (heavy terms)": design, the measured 10-09 run, R2 layout, runbook) and `specs/static-compaction.md` (design).

## Branches

| Branch (worktree) | SHA | What |
|---|---|---|
| `drill-append` (`wt/drill-append`) | `f252d8f6` | `cloud` @ `5c78b920` merged in, plus the drill code: `static_drill.py` and its tests, small hunks in `static_roots.py`, `static_names.py` (`r2-copy -x`), `static_append.py` (`merge_drills` in `publish`), `cli.py`, and the spec's drill sections. **The branch to land on `cloud`**: generic code only; `job/` is not part of it. |
| `compaction` (`wt/compaction`) | `dfc5152a` | Off `drill-append` before its last `cloud` merge (`cloud` @ `c7f547f3`). Adds `static_compact.py`, `tests/test_static_compact.py`, `specs/static-compaction.md`, a `write_run_shards` sub-plan naming fix in `static_append.py`, and a `TASK_ENV` prefix in `job/static-names.sh` (local driver only, not for `cloud`). Merge `drill-append` into it before going on. |

Each worktree has its own `.venv`: `UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --all-packages --all-extras --all-groups`. Tests: `cd cloud && ../.venv/bin/pytest -q tests/test_static_drill.py` (36 pass, ~2 min; drill), and `tests/test_static_compact.py` on `compaction` (11 pass; compaction).

## Done and verified

- **Drill runs.** The design, code and tests (exact equality: base ⊕ runs, each run and merged, = a rebuild for every member's roots and rollups; every view = brute force; mutations caught).
- **The real 2026-10-09 run** is on GCS and R2 under `static-names/2026-10-08c/deltas/2026-10-09/drill/`.
  - 653/653 views equal brute force from the 10-09 scan.
  - On 10-08, tiered equals base on 605/605.
  - 62 min on one spot n2-highmem-16; 2.15 GB.
  - Verification files are under `deltas/2026-10-09/drill-verify/`.
- **Speedups since that run, not yet measured at scale:**
  - new members' history via DuckDB (`history_table`; was 27 min);
  - settling from preloaded inputs;
  - probes bisecting cached groups;
  - newly heavy rows inserted as one Arrow table.
- **Compaction code and tests.**
  - After one run and after two, every light and drill file is byte-identical to a rebuild over the base's key ranges.
  - Real trial: folding `cintervals` for 256 ranges took 5 min wall (16 tasks). The shard merge took 19 min (32 tasks, 264 shards, 136.4 GB, ~0.7M rows/s per shard).
  - References matched: `ref-ranges` 8/8 files md5-equal to a rebuild from the 71 scan files (ranges 0, 77, 150, 255); `ref-shards` 6/6 (shards 1, 211, 233).

## Left

1. **The drill stage in `runs add`** (`static_runner.py` `Runner.one`). Put it after shards ∥ catalog and before publish. Make it one Batch job with 2 tasks: `python -m dt_cloud.static_drill build -g GEN -d SCAN_ID -k task` (task 0 long, task 1 short). It is done once `deltas/<id>/drill/meta.json` exists. The R2 job already copies `drill/` with each run (`R2_SERVED`). Add a fake-runner test in `tests/test_static_runner.py`.
2. **`publish` merges drill tiers.**
   - `merge_drills` is in place but untested end to end. Test it: publish-style merge (`merge_tiers`), then base ⊕ merged run = base ⊕ separate runs for every view. `test_static_drill.py`'s `merged` stack already checks it against the rebuild and brute force, so add the direct merged-vs-separate equality.
   - The cw agent was applying a quick fix ("no merging of runs that carry `drill/`"). Reconcile it, since `merge_drills` supersedes it.
   - `merge_drills` opens DuckDB at `connect(16, "100GB", tmp)`; publish runs on the profile's machine.
3. **T1236.**
   - Its manifest `manifests/2026-10-09T1236.json` lists the merged run `deltas/2026-10-09_2026-10-09T1236`, which has **no** `drill/`.
   - Build `deltas/2026-10-09T1236/drill/` (its prior tiers come out as base + `deltas/2026-10-09`).
   - Then the merged run's `drill/` = `merge_tiers` of the two. There's no CLI for that yet: add `runs drill merge -r <merged key>` over the per-scan runs of its `scans`.
   - Verify as for 10-09 (below), then R2 (`meta.json` last). Readers probe `<run>/drill/meta.json`, so no manifest is rewritten.
   - Note `?f=son&d=2610091236` needs the merged run's drill, not just T1236's.
4. **Time per scan.** 10-09 took 62 min serial: long 24 min, short 39 min. The long ∥ short split plus the speedups should bring the stage to about 20 min. Measure it on T1236. The short kind's probes (338K on 10-09) and newly heavy reads (12.7M rows) were the tail.
5. **Compaction** (`wt/compaction`; resume after 1–4):
   - The trial under `gs://oa-gcs-usage-scratch/static-names/compact-trial/` is **inconsistent**: T1236 was published mid-trial, so its `catalog/` folded T1236 while `scans.json` ends at 10-09. **Pin the run list** in `plan` (write it to `G'/compact-plan.json` and make every stage read it, not the newest manifest), then redo it from scratch under a new trial prefix.
   - Still to run: `catalog` → `drill-classes` → `drill-long` / `drill-short` (`-O` samples) → `ref-digests -O all` (32 tasks, ~$2) → `ref-aliases` → `ref-long` / `ref-short` on samples → report.
   - `finish` (indexes, manifest, `compacted.json`) only for a full fold.
   - A full fold is about $10 of Batch, plus about $52 of GCS → R2 egress if published. Ask before running it full.
   - Spend so far on compaction trials: about $1.5. On the drill task: about $1.5.

## Gotchas

- **`gcloud` / git**: this repo's git has `diff.noprefix` set, so `git apply` needs `-p0`. `cloud` is ambiguous as a revision (there's also a `cloud/` directory), so use `refs/heads/cloud`.
- **Batch with the `job/static-names.sh` on these branches** predates profiles. Prefix with `TASK_ENV=STATIC_NAMES_PROFILE=gcs` (on `compaction`), or use the profile-aware runner on `cloud`. Small stages run fine on `MACHINE=n2-highmem-4 SSD=375`. Task logs: `gcloud logging read 'logName="projects/oa-internal-450019/logs/batch_task_logs" AND labels.job_uid="<uid>"'` (`wt/compaction/tmp/joblog.sh JOB`).
- **R2**: use `wt/gcs-static`'s `job/static-names.sh r2-batch GEN/deltas/<id> -o drill/l` (then `drill/s`, `drill/a`, and last `drill/meta.json`), under `direnv exec ~/c/disky/wt/gcs`. Its `r2-copy` has no `-x`, and `-o` is relative to `-g`: pass `-g 2026-10-08c/deltas/<id>`, not `-o deltas/…`. Do the `-n` dry run first. Don't use the VM path.
- **Drill invariants the builder asserts**:
  - every heavy dir's kept children are named cells, and `kept = min(K, children)`; this needs `n_files ≥ 1` on every root, which holds on the real base;
  - a non-kept child with earlier roots only occurs in a dir with a remainder.
- **Delta header `kind` is −1** (it must sort before cells), not 3.
- **Run tiers have 2,048-row groups.** The reader's dispatch bound is `R + 2·Σ rg` over the tiers.
- **Heaviness is sticky** and counted by stored rows, so runs may hold rollups a rebuild wouldn't. Compaction recomputes heaviness from the rows, which drops those extras; it is tested.
- **Building the base layout in tests**: `_gen` in `test_static_catalog` plans its own key ranges. For compaction equality, rebuild over the base's ranges (`test_static_compact._gen_dir`).

## Commands that worked

```bash
# drill run for a scan (a trial to scratch first, then a server-side copy to deltas/<id>/drill/, meta.json last)
MODULE=static_drill SPOT=1 job/static-names.sh run build 1 -g 2026-10-08c -d 2026-10-09 -M 90GB -t gs://oa-gcs-usage-scratch/static-names/2026-10-08c/drill-trial/2026-10-09/drill
# verification
dt-cloud static-names runs drill cases -g 2026-10-08c -r <drill URL or run key> -t job/static-names/drill-terms.txt -n 6 -x 8 > cases.jsonl   # + job/static-names/drill-cases.jsonl
dt-cloud static-names runs drill query -g 2026-10-08c -r <…> -c cases.jsonl -d <id> -d <prev> > answers.jsonl      # and -r - for the base alone on <prev>
MODULE=static_roots SPOT=1 job/static-names.sh run drill-brute 1 -g 2026-10-08c -d <id> -c gs://…/cases.jsonl -k gs://…/answers.jsonl \
  -S gs://oa-gcs-usage-dvx/static-names/2026-10-08c/deltas/<id>/scans.json -O static-names/2026-10-08c/deltas/<id>/drill-verify/brute.jsonl
dt-cloud static-names roots drill-verify brute.jsonl answers.jsonl
# compaction trial stages (wt/compaction)
TASK_ENV=STATIC_NAMES_PROFILE=gcs MODULE=static_compact SPOT=1 job/static-names.sh run {plan 1|ranges 16 -n 16|shards-plan 1|shards 32|catalog 1|…} -g 2026-10-08c -t gs://oa-gcs-usage-scratch/static-names/compact-trial/<G'>
TASK_ENV=… MODULE=static_compact … run ref-ranges 1 -g 2026-10-08c -t <trial> -r <ref prefix> -O 0,77,150,255     # likewise ref-shards (-O shards with a ≥2-char common prefix)
```
