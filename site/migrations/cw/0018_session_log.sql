-- Session log (specs/session-log.md): optional, time-boxed user-action
-- logging. `POST /api/session-log` writes one `session_log_batches` row per
-- client flush (its events as a JSON array) and upserts the session's summary
-- row; `/admin/sessions` reads both. Off unless an admin turns it on at
-- `/admin` (`session_log_switch`, one row; each change also lands in
-- `admin_edits`).
--
-- Additive only: three new tables, nothing existing is altered. No foreign keys,
-- so the retention purge (`received_ts` older than `SESSION_LOG_RETAIN_DAYS`)
-- deletes from either table in any order. The gcs lineage carries the same
-- statements as `migrations/gcs/0042_session_log.sql`.
CREATE TABLE session_log (
  sid         TEXT PRIMARY KEY,   -- per-tab session id (client `sessionStorage`)
  email       TEXT,               -- the request's identity (server-derived); NULL = anonymous public viewer
  via         TEXT,               -- session | grant | public
  build       TEXT,               -- the client's build SHA
  ua          TEXT,               -- User-Agent
  started_ms  INTEGER NOT NULL,   -- earliest event (client clock, epoch ms)
  last_ms     INTEGER NOT NULL,   -- latest event
  n_events    INTEGER NOT NULL,
  n_errors    INTEGER NOT NULL,   -- error | reject | console events, and api events with status 0 or >= 500
  n_batches   INTEGER NOT NULL,
  first_url   TEXT,               -- the first batch's first `start` / `nav` URL
  received_ts INTEGER NOT NULL    -- epoch s (server clock) of the latest batch: what retention keys on
);
CREATE INDEX idx_session_log_received ON session_log (received_ts);
CREATE INDEX idx_session_log_email ON session_log (email, last_ms);

CREATE TABLE session_log_batches (
  sid         TEXT NOT NULL,
  pid         TEXT NOT NULL,      -- page-load id (a reload keeps `sid`, gets a new `pid`)
  seq         INTEGER NOT NULL,   -- the batch's number within its page load
  received_ts INTEGER NOT NULL,
  first_ms    INTEGER NOT NULL,
  last_ms     INTEGER NOT NULL,
  n           INTEGER NOT NULL,
  events      TEXT NOT NULL,      -- JSON array of `{t, k, ...}`, as validated
  PRIMARY KEY (sid, pid, seq)
);
CREATE INDEX idx_session_log_batches_received ON session_log_batches (received_ts);

-- The on/off switch: at most one row (`id = 1`). `until_ms` NULL = off; set =
-- on until that instant (epoch ms, exclusive), after which it's off on its own.
-- No row = off (the default).
CREATE TABLE session_log_switch (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  until_ms    INTEGER,            -- NULL = off
  who         TEXT NOT NULL,      -- admin who last set it
  ts          INTEGER NOT NULL    -- epoch s of that change
);
