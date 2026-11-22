-- A run stopped on purpose (`scan_runs.status = 'cancelled'`): terminal, never a failure. Until now a cancel recorded
-- `failed` with its reason in `error` (`cancelled from /scans`, Batch's reconcile `cancelled`), so every failure
-- count, red alert and ❌ took it in. SQLite can't widen a CHECK, so the table is rebuilt: same columns, constraints and
-- indexes, `status` admitting 'cancelled'. Its three children (`scan_run_dags`, `scan_run_outputs`, `scan_run_phases`)
-- reference it by name and keep their rows: the rows are set aside, the table dropped and recreated under its own
-- name, then refilled, with foreign keys checked at commit (`defer_foreign_keys`): the refill resolves every child the
-- drop orphaned. (Not create-new-then-rename: rows copied in before the drop don't count against the drop's orphans,
-- and the commit fails.) After `scan-run-thread-url` (it copies `thread_url`).
-- The rows a cancel wrote as failed become cancelled, unless a step had already failed on its own before the cancel:
-- that run's failure is real, and it stays failed. Not `failed_phase`: a cancel records the step it killed there (and
-- `reconcile` closes it `failed (lost)`), so a step that failed on its own is one with another note that ended before
-- the run did.
PRAGMA defer_foreign_keys = true;
CREATE TABLE scan_runs_cancelled_copy AS SELECT * FROM scan_runs;
DROP TABLE scan_runs;
CREATE TABLE scan_runs (
  run_id       TEXT PRIMARY KEY,
  store        TEXT NOT NULL DEFAULT 'primary',
  scan         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  parent       TEXT,
  status       TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'nop', 'cancelled')),
  failed_phase TEXT,
  error        TEXT,
  started_ts   INTEGER,
  finished_ts  INTEGER,
  job_name     TEXT,
  region       TEXT,
  machine      TEXT,
  spot         INTEGER,
  tasks        INTEGER,
  image        TEXT,
  cost_usd     REAL,
  gen          TEXT,
  source       TEXT NOT NULL CHECK (source IN ('job', 'backfill')),
  updated_ts   INTEGER NOT NULL,
  trigger      TEXT CHECK (trigger IN ('schedule', 'manual')),
  trigger_by   TEXT,
  thread_url   TEXT
);
INSERT INTO scan_runs (run_id, store, scan, kind, parent, status, failed_phase, error, started_ts, finished_ts, job_name, region,
                       machine, spot, tasks, image, cost_usd, gen, source, updated_ts, trigger, trigger_by, thread_url)
  SELECT run_id, store, scan, kind, parent, status, failed_phase, error, started_ts, finished_ts, job_name, region,
         machine, spot, tasks, image, cost_usd, gen, source, updated_ts, trigger, trigger_by, thread_url
  FROM scan_runs_cancelled_copy;
DROP TABLE scan_runs_cancelled_copy;
CREATE INDEX idx_scan_runs_parent ON scan_runs (parent);
CREATE INDEX idx_scan_runs_scan ON scan_runs (store, scan);
CREATE INDEX idx_scan_runs_started ON scan_runs (store, started_ts);
UPDATE scan_runs SET status = 'cancelled' WHERE status = 'failed' AND error IN ('cancelled', 'cancelled from /scans')
  AND NOT EXISTS (SELECT 1 FROM scan_run_phases p WHERE p.run_id = scan_runs.run_id AND p.status = 'failed'
                  AND COALESCE(p.note, '') <> 'failed (lost)' AND p.finished_ts < scan_runs.finished_ts);
