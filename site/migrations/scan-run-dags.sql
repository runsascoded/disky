-- cloud's `scan-run-phase-jobs` + `scan-run-dags` + `scan-run-dags-neutral` (applied on cloud, then removed), as one
-- recipe for this branch's databases: a phase's own Batch job, machine and pipeline node (NULL for a one-VM run), and
-- each run's DAG, engine-neutral (`dt_cloud.run_dag`).
ALTER TABLE scan_run_phases ADD COLUMN job_name TEXT;
ALTER TABLE scan_run_phases ADD COLUMN machine TEXT;
ALTER TABLE scan_run_phases ADD COLUMN task TEXT;
CREATE TABLE scan_run_dags (
  run_id  TEXT PRIMARY KEY REFERENCES scan_runs(run_id),
  engine  TEXT NOT NULL,
  dag     TEXT NOT NULL
);
