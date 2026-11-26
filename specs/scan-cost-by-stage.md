# A scan's cost by stage and job, estimated live and billed a day later

Status: design (2026-11-26, the cw session; Ryan: "the scans page should let us see effective cost #'s per stage/phase/step", and "sg" to this shape and the naming below). [cloud].

## Why

/scans shows one cost per run: the head job's own estimate plus its Nextflow tasks (`scan_runs.labelled_cost`, by `nf-session`), at list price. What it misses:

- **Jobs a task submits.** static-names' `append`, `catalog`, `shards`, `drill`, `anchors`, `r2`, `merge`, `compact` carry `scan_id` but neither `scan_run` nor `nf-session` (the Nextflow tasks have no `SCAN_RUN_ID` in their env, so `cost_labels.label_batch_spec` has nothing to add). On cw that's the biggest line: GCP's billing export for the 14 days to 2026-11-26, scaled to a month, has static-names (append, drill, anchors, folds) at ~$700 against the scans' own ~$290. cw's /scans cost is about a third of its compute.
- **The breakdown.** One number per run; nothing shows a step growing (cw's `anchors` went from 20–40 min to 100–155 min a scan over 11-17 → 11-25, visible only in logs).
- **What was billed.** The estimate is list price × VM hours; spot prices, credits, disks and transfer differ, and the billing export has them, a day late.

## Terms

The common hierarchy (Azure Pipelines: pipeline → stage → job → step; Jenkins: pipeline → stage → step; GitLab: pipeline → stage → job; GitHub Actions: workflow → job → step), applied to ours:

| term | ours | today's name |
|---|---|---|
| run | one execution of a scan's pipeline (`scan_run`); a scan (`scan_id`) can have several: retries, catch-ups | run |
| stage | a coarse phase of the run: list → index → stores (interval store, static names) → publish → post | `STEPS[n].stage` already; a static-names job's `stage` label is a job, below |
| job | a Batch job: one machine boundary, what's billed | a Nextflow task; a static-names `stage` (`append`, `drill`, …) |
| step | a registered unit of work (`@step`), run inside a job; the `post` job runs several | step |
| section | a marked span inside one job's log (`resource_stage`: the drill's `long: settle`) | "stage" in `resource_stage` |

"Phase" goes from the UI: `scan_run_phases` records jobs and steps (its D1 name stays; renaming a table is a D1 change per database).

## Attribution

1. Every job a run causes carries its `scan_run` (and `scan_id`):
   - the scan head passes `SCAN_RUN_ID` (its own Batch uid, or the one it was given) into every Nextflow task's env, and `scan_run` into `process.resourceLabels` beside `nf-session` and `step`;
   - submitters inside a task already forward `$SCAN_RUN_ID` (`job/static-names.sh` `task_env`, `cost_labels.label_batch_spec`), so the static-names jobs get it with no change of their own;
   - the labels keep their names: a job's `stage` label (static names' `append`, `drill`, …; `scan_tasks`' step) is its *job* kind in these terms, and its `purpose` / `component` (`static-names`, `interval-store`, `scan`) its stage. The readers (`scan_cost`, /scans) map them; renaming the labels would break the billing export's history and every filter on them for no new information.
2. **Store upkeep is the generation's, not the scan's.** A carry or a fold serves every scan it merges (cw's pending fold: 49), so it carries `gen` and `upkeep: carry|fold` (`append_runner.UPKEEP_LABEL`, on the job and its allocation) beside the submitting run's `scan_run`; `scan_cost.report` lists it as `upkeep_usd`, outside the run's total, and `finish -l` leaves it out of the run's recorded cost. The R2 copy of a carry's revision (minutes) stays the run's. Both stores' Batch carries and folds are covered (the interval store's `Runner` is the same `append_runner.Runner`); open: a carry or fold done in-process inside a scan task (`interval-store add` in cw's own task) is in that task's VM time, which labels can't split.
3. `scan_cost.select_jobs`: by `scan_run` (as now); the `legacy` path stays for jobs from before.

## Estimate, then billed

- **Live estimate** (as now, per job): the machine's price × its VM hours × tasks (`job_fields`), spot at `spot_factor`. Computed when read, from each job's stored machine and times (`scan_run_phases` has `job_name` and `machine`): no D1 change.
- **Billed**: the daily `gcp-cost` step (already the deployment's primary, once a day) also queries the billing export for the last few days' Batch lines grouped by `scan_run`, `stage`, `job` and `upkeep` (`labels`), writing `snapshots/cost/runs/<date>.json` (`{scan_run: {stage: {job: {usd, credits, vm_h}}}}`), the way it writes today's snapshot. No D1 change. The site reads the newest few and shows billed where it has it, the estimate elsewhere, marked which.

## On /scans

- A run's detail: stage → job rows (machine, VM-h, estimate, billed, the job's link), upkeep lines apart, the run's total as estimate + billed.
- The table's cost column: billed when known, else the estimate (marked).
- A per-stage cost chart across runs (and VM-h, for what spot prices hide), so a stage that starts growing shows.

## Order

1. Attribution: the head's task env and labels (done: `scan_run`, `SCAN_RUN_ID`; `finish -l scan_run=`), then `upkeep` on carries and folds, apart in `scan_cost.report` and `finish`.
2. The run detail's stage → job rows from the estimate.
3. `gcp-cost`'s billed snapshot and the site reading it.
4. The per-stage chart; the UI's wording (stage, job, step; no "phase").
