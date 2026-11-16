-- Who started a manual scan run (`scan_runs.trigger_by`, `$DT_TRIGGER_BY`): the site's "Run a scan" dispatcher's
-- address, or what a hand submit names. NULL: unknown, or not a manual run.
ALTER TABLE scan_runs ADD COLUMN trigger_by TEXT;
