# Cost labels: required, not opt-in

Status: implemented on `cloud` (10-25); the deployment branches' shell submits and the audit remain (below).

Ryan (10-25): "everything any disky deploy does should be labeled … it should default on from US, and error if appropriate label md isn't set."

## Why

gcs's billing export (10-19 → 10-24) has many Batch jobs with no `deployment`/`component` label. Most were race and probe jobs, plus their helpers:
- `sn-compact-probe-*`, `sn-measure-shards-*`, `sn-race-c001-*`;
- `sn-measure-probe-*`, `sn-probe-*`;
- `gcs-iv-repair-*`, `gcs-iv-r2copy-*`;
- some `sn-anchors-*` / `sn-anchor-shard-*` helpers.

They show up only as `batch-job-id`, so a deployment's real cost has to be rebuilt by hand, job name by job name.

Root causes:
1. **Labels are opt-in.** `cost_labels.label_batch_spec` returns the spec unchanged when `$DISKY_LABELS` is unset, so a job submitted from an env without it is silently unlabeled. A race head (`compact-probe`) had no `DISKY_LABELS`, so every helper it fanned out was unlabeled too.
2. **The env doesn't reach grandchildren.** It's forwarded into a child job's task env only when `scan_run`/`scan_id` labels are present. A fan-out under a non-scan driver loses it.
3. **Shell submits bypass Python.** `gcloud batch jobs submit --config spec.json` in deployment `job/*.sh` scripts and ad-hoc race scripts goes around `label_batch_spec` entirely.

## Requirements

1. **Required labels.** Every Batch job any disky code creates must carry `app`, `deployment` and `component`, both on the job (`labels`) and on `allocationPolicy.labels`.
   - `cost_labels` / `label_batch_spec` raise (e.g. `MissingCostLabels`, naming the missing keys) when `$DISKY_LABELS` lacks `app` or `deployment`, or no component is given.
   - There is no unset-means-unlabeled path and no opt-out flag. Tests set the env.
   - Remove the module docstring's "Unset `DISKY_LABELS` = no labels at all" contract.
2. **One choke point.** `batch.submit_job` (and any other `jobs.create` call, e.g. the fan-out helpers in `static_fanout.py` and `compact-probe`'s seeding) validates the final spec right before the network call, and raises if a required label is missing on either level.
   - A test asserts that every module that creates Batch jobs goes through it, e.g. a grep-style test that no other `batch_v1` / `gcloud batch jobs submit` call site exists in `cloud/src`.
3. **Inheritance.** `label_batch_spec` always forwards `DISKY_LABELS` into the child's task env, not only when scan labels are present. Any job that may itself submit helpers (head, compaction, compact-probe, measure/anchor/write/fold shards) then labels its grandchildren identically.
   - The child's `component` stays its own; `stage`/`purpose` keep working as today.
4. **A CLI for shell submits.** `dt-cloud batch submit -c COMPONENT [-L REGION] [-p PROJECT] JOB_ID SPEC.json` labels the spec from `$DISKY_LABELS` + `-c`, validates it (requirement 1), and submits it.
   - The deployment branches switch their `gcloud batch jobs submit` lines to it. gcs will do `job/batch-submit.sh`, `job/static-names.sh`, `job/access-submit.sh` and its race tooling; cw/hf their own.
   - It is also the documented way to submit ad-hoc jobs (races, analysis), so they're attributable.
5. **Nextflow.** `scan head` refuses to start unless the profile env's `DISKY_LABELS` has `app` + `deployment`. `process.resourceLabels` already merges `params.labels`; make sure `params.labels` always includes them, plus `component` (e.g. `scan`) next to the existing `step`.
6. **Audit.** `dt-cloud cost-labels audit [-d DAYS] -t BILLING_TABLE` lists the window's Compute Engine cost lines that have a `batch-job-id` but no `deployment` label (job id, first day, net $) and exits nonzero if any are found.
   - It's read-only BigQuery. A deployment can run it in its digest or a weekly check.
   - Non-Batch VMs (people's dev boxes in a shared project) aren't disky's, so they're out of scope.

## Out of scope / notes

- **Cloud Build:** builds have tags, not billing labels. Its ~$1–2/day stays unattributed unless builds move to a labeled Batch job; not worth it now.
- **Buckets:** already labeled (`shared`/`data`, `cw-s3`/`data`).

## Implementation (10-25, `cloud`)

- **Required labels** (`cost_labels.py`): `MissingCostLabels` (a `ValueError`; `.missing`, `.where`) names the missing keys, e.g. `cost labels: missing app, deployment ($DISKY_LABELS carries app=…,deployment=…; each job names its component)`. `deployment_labels(env)` requires `app` + `deployment` (non-empty); `cost_labels(component, env)` adds `component` (required) and the scan context (`scan_run` / `scan_id`). `env_labels` still parses whatever is set; nothing returns `{}` for a job any more.
- **Choke point:** `batch.submit_job(spec, job_id, region, project=None)` runs `check_spec` (both `labels` and `allocationPolicy.labels` carry `app`, `deployment`, `component`) before the POST. Every Batch create in `cloud/src` already went through it: `BatchRunner` (static names, interval store, every fan-out helper: `measure_fanout` / `anchor_fanout` / `fold_fanout`, `compact-probe`'s through `_helper_runner`), `scan task`, `job submit-listing`, `job submit-reviewed`. No module used `batch_v1` or `gcloud batch jobs submit`. `test_one_batch_create_call_site` walks `cloud/src` with `ast`: the only function that both names `batch.googleapis.com` and POSTs is `batch.submit_job`, and no line matches the client library or a `gcloud … batch jobs submit` (string or argv form). `sweep_batch`'s Storage Batch Operations jobs are another API (no VMs) and stay out of scope.
- **Inheritance:** `label_batch_spec` always writes `$DISKY_LABELS` (raw) into every task group's env, plus `$SCAN_RUN_ID` / `$SCAN_ID` when set. `stage` reaches `allocationPolicy.labels` under a scan's context only, as before.
- **`dt-cloud batch submit -c COMPONENT [-L REGION] [-p PROJECT] JOB_ID SPEC_FILE`** (`-` = stdin): `label_batch_spec` (env + `-c`), then `submit_job`, printing the job's name.
- **Nextflow:** `scan head` takes `DISKY_LABELS` from the profile's `env` when its own env lacks it (as `D1_DB_ID` & co.), then refuses (`deployment_labels`) before writing or recording anything. `params.labels` = profile `labels` + `$DISKY_LABELS` + `component: scan` + `scan_id`; `params.env` always carries `DISKY_LABELS`, so a google-batch step that submits Batch jobs (static names, the interval store) labels them.
- **Audit:** `dt-cloud cost-labels audit [-d DAYS] [-p PROJECT] -t BILLING_TABLE` (default 7 days) runs `audit_sql` as a BigQuery job over REST (`query_rows`, ADC; `-p` is the project the query bills to). It prints `job_id<TAB>first_day<TAB>$net` per job and a total line, sorted by net desc, and exits 1 when any are found. `net` = `cost` + credits. The window prunes by `_PARTITIONTIME` (the export is ingestion-time partitioned, so a line's partition is never before its usage day).
- `dt-cloud cost-labels [-c] [-j]` is now a group (`invoke_without_command`) and still prints labels; without `-c` it raises (`component` missing).
- `deploy/sheet-mirror/deploy.sh` always passes `--update-labels "$(dt-cloud cost-labels -c sheet-sync)"`: an unset `$DISKY_LABELS` fails the deploy.
- **Tests:** `conftest` sets `DISKY_LABELS=app=disky,deployment=test` for every test. Exact-spec tests now include the labels and the forwarded env. `test_cost_labels.py` covers:
  - the missing-label raises (env, component, `check_spec`, `submit_job` before the network);
  - the choke-point walk;
  - grandchild inheritance without a scan context;
  - the `batch submit` round trip (the exact spec `submit_job` receives);
  - `audit_sql` / `audit_rows` / `audit_lines` and the `audit` CLI's exit codes.

  `test_scan_head.py` covers `head`'s refusal and the profile-env fallback.

## Deployment follow-ups (not on `cloud`)

- gcs: switch `job/batch-submit.sh`, `job/static-names.sh`, `job/access-submit.sh` and its race tooling from `gcloud batch jobs submit --config` to `dt-cloud batch submit -c <component>`. Make sure every job env that runs dt-cloud (heads, race drivers, laptop drivers) exports `DISKY_LABELS`.
- cw / hf: the same for their own submit scripts; their scan profiles (or the Batch head's env) carry `DISKY_LABELS`.
- Each deployment: run `dt-cloud cost-labels audit -d 2 -t <billing export>` after a day with a race; it should be clean.

## Done when

- `cloud` tests cover:
  - the missing-label raise;
  - the choke-point grep;
  - grandchild env inheritance;
  - the CLI round trip.
- After the deployment branches merge it, `cost-labels audit -d 2` over a day with a race is clean.
