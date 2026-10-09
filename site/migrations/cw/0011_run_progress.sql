-- A sweep run's progress history (dt-cloud's D1 samples; the site's /runs/<id> charts): the cw lineage's copy of gcs's 0035.
CREATE TABLE deletion_progress (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  bucket TEXT NOT NULL,
  ts REAL NOT NULL,
  deletes INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  gone INTEGER NOT NULL,
  overwritten INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  done INTEGER NOT NULL,
  bytes_exact INTEGER NOT NULL,
  PRIMARY KEY (run_id, bucket, ts)
);
