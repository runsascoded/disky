ALTER TABLE deletion_runs ADD COLUMN selected_items_json TEXT;
ALTER TABLE deletion_runs ADD COLUMN executor_started_ts INTEGER;
ALTER TABLE deletion_runs ADD COLUMN execution_errors_json TEXT;
