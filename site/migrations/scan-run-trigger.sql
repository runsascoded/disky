-- What started each scan run (`dt-cloud scan-run start`, `$DT_TRIGGER`): the deployment's cron (`schedule`) or anything
-- else (`manual`); NULL for runs recorded before it (and backfills).
ALTER TABLE scan_runs ADD COLUMN trigger TEXT CHECK (trigger IN ('schedule', 'manual'));
