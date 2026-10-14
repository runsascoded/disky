-- Scan runs (specs/scan-runs-ui.md): one row per job run that produced or
-- touched a scan — the orchestrating scan job, a REPROC, a listing fan-out
-- job, a downstream index run — with its phase timings and the outputs it
-- wrote. `dt-cloud scan-run record` writes these from the job (per phase, and
-- from its exit trap), `dt-cloud scan-run backfill` reconstructs past runs
-- from the Batch API, Cloud Logging and the data bucket; `/scans` reads them.
--
-- Additive only: three new tables, nothing existing is altered or rebuilt.
-- The two child tables reference `scan_runs` (D1 enforces foreign keys), so
-- writers upsert the parent first and never `INSERT OR REPLACE` it (a REPLACE
-- deletes the row, which its children's FKs refuse). Nothing references
-- `index_schema`: a run's `gen` is a plain column.
CREATE TABLE scan_runs (
  run_id       TEXT PRIMARY KEY,                -- the Batch job uid ($BATCH_JOB_UID); else `<scan>@<started_ts>`
  store        TEXT NOT NULL DEFAULT 'primary', -- 'primary' or a `STORES_JSON` key (a secondary store's own job)
  scan         TEXT NOT NULL,                   -- scan id (YYYY-MM-DD or YYYY-MM-DDTHHMM)
  kind         TEXT NOT NULL,                   -- the profile's job kind: scan | reproc | listing | static-names | …
  parent       TEXT,                            -- the orchestrating run of a downstream job (no FK: either may land first)
  status       TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'nop')),
  failed_phase TEXT,                            -- the phase running when it failed
  error        TEXT,                            -- the failure's one-line reason (exit code + command, or Batch's event)
  started_ts   INTEGER,                         -- epoch s: the job's task started (RUNNING)
  finished_ts  INTEGER,
  job_name     TEXT,                            -- the Batch job id (its name's last segment)
  region       TEXT,
  machine      TEXT,
  spot         INTEGER,                         -- 1 = SPOT provisioning
  tasks        INTEGER,
  image        TEXT,
  cost_usd     REAL,                            -- estimate: the profile's $/h for the machine × run hours × tasks
  gen          TEXT,                            -- the index generation the run wrote, if any
  source       TEXT NOT NULL CHECK (source IN ('job', 'backfill')),
  updated_ts   INTEGER NOT NULL
);
CREATE INDEX idx_scan_runs_scan ON scan_runs (store, scan);
CREATE INDEX idx_scan_runs_started ON scan_runs (store, started_ts);
CREATE INDEX idx_scan_runs_parent ON scan_runs (parent);

-- A run's phases, in order: each spans [started_ts, finished_ts] (the job's
-- `PHASE <name>: <s>s` markers are cumulative wall clock, so a phase starts
-- where the previous one ended).
CREATE TABLE scan_run_phases (
  run_id      TEXT NOT NULL REFERENCES scan_runs(run_id),
  phase       TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  started_ts  INTEGER,
  finished_ts INTEGER,
  status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('running', 'done', 'failed')),
  note        TEXT,
  PRIMARY KEY (run_id, phase)
);

-- What a run wrote, per structure (`listing`, `listing/<bucket>`, `dir-cache`,
-- `path-index/<tier>`, `snapshot`, `static/<run>`, …): measured from the store
-- after the phase that wrote it. Deltas are not stored: the reader diffs a key
-- against the previous scan's run.
CREATE TABLE scan_run_outputs (
  run_id  TEXT NOT NULL REFERENCES scan_runs(run_id),
  key     TEXT NOT NULL,
  uri     TEXT NOT NULL,                        -- gs:// | r2:// | s3:// prefix or object
  bytes   INTEGER,
  objects INTEGER,
  rows    INTEGER,
  gen     TEXT,
  PRIMARY KEY (run_id, key)
);
