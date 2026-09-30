-- Link executed runs to the first-class plan they ran (specs/gcs-toward-union.md
-- §5, seam 1), and carry the CAIOS purge stage so one `deletion_runs` shape
-- serves both clouds (cw migration 0003 defined these inline; gcs adds them to
-- its live table). Both nullable/defaulted: gcs's four pre-plan real sweeps
-- (run_id predating plans) keep plan_id NULL, and purge_state is meaningful
-- only where a real run leaves noncurrent versions to GC (CAIOS) — on GCS the
-- soft-delete window is the net and purge stays 'none'.

ALTER TABLE deletion_runs ADD COLUMN plan_id INTEGER REFERENCES plans(id);
ALTER TABLE deletion_runs ADD COLUMN purge_state TEXT NOT NULL DEFAULT 'none'
  CHECK (purge_state IN ('none', 'pending', 'done'));
