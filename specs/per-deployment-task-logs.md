# Per-deployment Batch task logs

**Status:** draft, 2026-11-27. Ryan's asks (cw session): task logs kept 365 days, each deployment's in a bucket of its own (IaC'd in its own stack); no shared bucket.

## Today

- Every deployment's Batch jobs (gcs, cw, hf, oa-r2) run in one GCP project, `oa-internal-450019`.
- Their task logs go to `_Default` (30 days) and, through a project-wide sink in gcs's stack (`TaskLogBucket`, filter `log_id("batch_task_logs")`), to `batch-task-logs` (90 days, every deployment's logs).
- A job's account can't read another job's logs: cw's compaction got 403 reading its helpers' (`job_watch`), as reading needs `roles/logging.viewer` on the whole project, which would expose every deployment's logs.

## The label

Every task log entry carries its job's labels (`labels.deployment`, `labels.component`, `labels.stage`, …), Nextflow tasks and site-submitted jobs included: of 20,000 entries on 2026-11-27, cw 6,031, gcs 1,453, hf 6,547, oa-r2 5,969, none unlabelled. `batch.submit_job` refuses a job without `deployment` (cost labels). So routing is by label, not by job name (Nextflow's are `nf-<hash>-<ts>` on every deployment).

## Design

1. **`[cloud]` `infra/gcp/task_logs.py`:**
   - `TaskLogBucket(deployment=D)`: its sink's filter is `log_id("batch_task_logs") AND labels.deployment="D"`.
   - `TaskLogView(deployment=D)`: the view's filter is the same, an alternative to `job_uid_prefix`.
2. **`[cloud]` reading through a deployment's view:**
   - `$DT_TASK_LOG_VIEW` (`projects/P/locations/L/buckets/B/views/V`) is the `resourceNames` of every task-log read: `gcp.task_log_resource()`, used by the `task_log_filter` callers (`job_backend`, `sweep_progress`, `dt-cloud job logs`, `scan_runs_backfill`).
   - Unset, they read the project, as today.
   - `batch.submit_job` forwards it into each task's env, beside `$DISKY_LABELS`, so a job's helpers read through the same view.
3. **Per deployment, in its own stack:**
   - a bucket `<d>-task-logs`, retention 365 days, its sink filtered to its own label;
   - a view on it, with `roles/logging.viewAccessor` for its job account (and dispatch accounts that read logs);
   - `DT_TASK_LOG_VIEW` in its submitters' env (scheduled scans, the static-names and interval-store launchers, the site's dispatch).
4. **gcs** retires the project-wide `batch-task-logs` sink and bucket once every deployment has its own; until then logs are copied into both.

`_Default` keeps its 30 days, so the Logs Explorer works as now.

## Tests

- `TaskLogBucket` / `TaskLogView` filters (the Pulumi mocks).
- `task_log_resource()` with and without the env.
- `submit_job` forwarding `$DT_TASK_LOG_VIEW` into the task env when set (and only then).
