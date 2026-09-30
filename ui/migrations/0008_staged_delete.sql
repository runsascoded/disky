-- Staged delete (spec `specs/staged-delete.md`, CP2): the D1 mirror of the CP1
-- SQLite models (`src/disk_tree/sqla/deletion.py`). The opt-in "delete" model —
-- nothing dies by inaction. A shared open `plan` collects `plan_items` (trash
-- gestures); an admin dispatch closes the plan and records a `deletion_run` the
-- server-side executor drains (CP4), each item landing a `deletion_band`.
--
-- Timestamps are epoch seconds (INTEGER), matching `grants`/`access_log`: they
-- compare and sort cheaply and share the auth stack's convention.

-- A staged set: auto-created open on the first stage, closed by a real dispatch.
CREATE TABLE plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open',   -- open | closed
  created_by TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  closed_ts  INTEGER
);

CREATE INDEX plans_open ON plans (state, id DESC);

-- The staged URIs of a plan (idempotent per URI).
CREATE TABLE plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
  uri      TEXT NOT NULL,
  note     TEXT,
  added_by TEXT NOT NULL,
  added_ts INTEGER NOT NULL,
  PRIMARY KEY (plan_id, uri)
);

-- One execution of a plan. `finished_ts IS NULL` = enqueued, awaiting the
-- server-side executor (the edge can't reach arbitrary buckets; CP4 drains).
CREATE TABLE deletion_runs (
  run_id          TEXT PRIMARY KEY,
  plan_id         INTEGER NOT NULL REFERENCES plans (id),
  mode            TEXT NOT NULL,               -- dry | real
  actor           TEXT NOT NULL,
  started_ts      INTEGER NOT NULL,
  finished_ts     INTEGER,
  deleted_bytes   INTEGER NOT NULL DEFAULT 0,
  deleted_objects INTEGER NOT NULL DEFAULT 0,
  skipped_gone    INTEGER NOT NULL DEFAULT 0,
  undo_state      TEXT NOT NULL DEFAULT 'none', -- none | partial | full | expired
  undo_deadline   INTEGER
);

CREATE INDEX deletion_runs_started ON deletion_runs (started_ts DESC);
CREATE INDEX deletion_runs_plan ON deletion_runs (plan_id, started_ts DESC);

-- The per-item result of a run.
CREATE TABLE deletion_bands (
  run_id  TEXT NOT NULL REFERENCES deletion_runs (run_id) ON DELETE CASCADE,
  uri     TEXT NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  objects INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  gone    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, uri)
);
