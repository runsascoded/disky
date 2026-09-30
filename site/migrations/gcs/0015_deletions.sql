-- Deletion history — executed sweeps as first-class records
-- (specs/sweep-executor.md § Deletion records). Marks are intent; these are
-- fact. One `deletion_runs` row per executor invocation; `deletion_bands`
-- aggregates it per covering band prefix (the queryable per-path unit — a
-- path's deletion history = bands ancestor-or-equal to it, plus bands under
-- it for "deletions of descendants"). Object-level detail stays in the run's
-- log parquets (`log_dir`), which the /files browser renders.

CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY,           -- <scan>-h<head>/<utc compact ts>
  plan TEXT NOT NULL,                -- gs:// plan dir consumed
  scan TEXT NOT NULL,
  head INTEGER NOT NULL,             -- ledger head the plan was pinned to
  exec_head INTEGER NOT NULL,        -- ledger head at execution (re-verified)
  actor TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('dry', 'real')),
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  deleted_bytes INTEGER NOT NULL DEFAULT 0,   -- would-delete bytes for dry runs
  deleted_objects INTEGER NOT NULL DEFAULT 0,
  skipped_gone INTEGER NOT NULL DEFAULT 0,
  skipped_overwritten INTEGER NOT NULL DEFAULT 0,
  drift_dirs INTEGER NOT NULL DEFAULT 0,      -- dirs skipped: new keys since the scan
  ledger_drift_dirs INTEGER NOT NULL DEFAULT 0, -- dirs dropped: newer marks
  undo_deadline INTEGER,             -- finished_ts + the bucket soft-delete window (real runs)
  undo_state TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  log_dir TEXT NOT NULL              -- gs:// dir holding {would-delete,deleted}/ parquets + summary
);

CREATE TABLE deletion_bands (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  prefix TEXT NOT NULL,              -- gs://<bucket>/<dir>/ band the deletions fell under
  bytes INTEGER NOT NULL,
  objects INTEGER NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  overwritten INTEGER NOT NULL DEFAULT 0,
  drift_new_objects INTEGER NOT NULL DEFAULT 0,
  undone_objects INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);

-- Per-path lookups: covering bands via the ancestor IN-list (like keep_prefixes),
-- descendant listings via the prefix range scan — both want this index.
CREATE INDEX idx_deletion_bands_prefix ON deletion_bands (prefix);
