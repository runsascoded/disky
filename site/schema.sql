-- The D1 schema: this branch's current state (specs/done/d1-migrations.md). A new database is created from it
-- (`wrangler d1 execute <db> --remote --file site/schema.sql`); a change lands as a recipe in `site/migrations/`
-- plus this file, regenerated with `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql`.
-- Canonical form (`dt-cloud d1-schema dump`): edit through a recipe, not by hand.

CREATE TABLE access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  event       TEXT NOT NULL,
  grant_id    TEXT,
  session_sub TEXT,
  path        TEXT,
  status      INTEGER,
  ip_hash     TEXT,
  ua          TEXT,
  country     TEXT,
  referer     TEXT,
  reason      TEXT,
  bucket      INTEGER
, city TEXT, region TEXT, as_org TEXT);

CREATE TABLE access_log_daily (
  day      INTEGER NOT NULL,
  event    TEXT NOT NULL,
  grant_id TEXT,
  path     TEXT,
  country  TEXT,
  events   INTEGER NOT NULL,
  clients  INTEGER NOT NULL,
  PRIMARY KEY (day, event, grant_id, path, country)
);

CREATE TABLE access_requests (
  id         TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  name       TEXT,
  note       TEXT,
  created_at INTEGER NOT NULL,
  status     TEXT NOT NULL,
  decided_at INTEGER,
  decided_by TEXT,
  grant_id   TEXT,
  ip_hash    TEXT
, subject_json TEXT);

CREATE TABLE admin_edits (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  action   TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
  who      TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  old_json TEXT,
  new_json TEXT
);

CREATE TABLE admin_emails (
  email TEXT PRIMARY KEY,
  note  TEXT,
  who   TEXT NOT NULL,
  ts    INTEGER NOT NULL
);

CREATE TABLE agent_tokens (
  email    TEXT PRIMARY KEY,
  grant_id TEXT NOT NULL,
  created  INTEGER NOT NULL
);

CREATE TABLE agents (
  name TEXT PRIMARY KEY,
  seen_ts INTEGER NOT NULL,
  host TEXT
);

CREATE TABLE allowed_emails (
  email TEXT PRIMARY KEY,
  note  TEXT,
  who   TEXT NOT NULL,
  ts    INTEGER NOT NULL
, read_only INTEGER NOT NULL DEFAULT 0);

CREATE TABLE footer_cache (
  date            TEXT NOT NULL,
  variant         TEXT NOT NULL,
  gen             TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('filling', 'hot', 'evicting')),
  rows            INTEGER NOT NULL DEFAULT 0,
  bytes           INTEGER NOT NULL DEFAULT 0,
  started_ts      INTEGER NOT NULL,
  filled_ts       INTEGER,
  last_access_day INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, variant, gen)
);

CREATE TABLE grants (
  id            TEXT PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,
  name          TEXT,
  note          TEXT,
  subject_json  TEXT,
  email         TEXT,
  scopes        TEXT NOT NULL,
  max_redeems   INTEGER,
  redeems       INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER,
  session_ttl   INTEGER,
  created_at    INTEGER NOT NULL,
  created_by    TEXT NOT NULL,
  revoked_at    INTEGER,
  first_used_at INTEGER,
  last_used_at  INTEGER
, disabled_at INTEGER, expiry_ends_sessions INTEGER NOT NULL DEFAULT 1, sessions_invalid_before INTEGER);

CREATE TABLE hosts (
  id TEXT PRIMARY KEY,
  store TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  home TEXT,
  root TEXT,
  mark TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  created_by TEXT,
  revoked_at TEXT
, grant_id TEXT, os TEXT, version TEXT, seen_at TEXT);

CREATE TABLE index_row_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL,
  gen       TEXT NOT NULL,
  rg        INTEGER NOT NULL,
  d_min     INTEGER NOT NULL,
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,
  u_min     TEXT,
  u_max     TEXT,
  row_start INTEGER NOT NULL,
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL, store TEXT NOT NULL DEFAULT 'primary',
  PRIMARY KEY (date, variant, gen, rg)
);

CREATE TABLE "index_schema" (
  store       TEXT NOT NULL DEFAULT 'primary',
  date        TEXT NOT NULL,
  variant     TEXT NOT NULL DEFAULT 'path',
  version     INTEGER NOT NULL,
  schema_json TEXT NOT NULL,
  floor_bytes INTEGER,
  gen         TEXT,
  dir         TEXT, started TEXT,
  PRIMARY KEY (store, date, variant)
);

CREATE TABLE og_tokens (
  id         INTEGER PRIMARY KEY,
  token      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  view       TEXT NOT NULL,
  page       TEXT NOT NULL,
  minted_by  TEXT NOT NULL,
  minted_ts  INTEGER NOT NULL,
  exp_day    INTEGER NOT NULL,
  revoked_by TEXT,
  revoked_ts INTEGER
);

CREATE TABLE pending_auth (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL,
  token_hash  TEXT NOT NULL,
  code_hash   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0
, ip_hash TEXT);

CREATE TABLE plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_by TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  closed_ts  INTEGER
, slack_channel TEXT, slack_ts TEXT);

CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id),
  manifest TEXT NOT NULL,
  scan TEXT NOT NULL,
  head INTEGER NOT NULL,
  exec_head INTEGER NOT NULL,
  actor TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('dry', 'real')),
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  deleted_bytes INTEGER NOT NULL DEFAULT 0,
  deleted_objects INTEGER NOT NULL DEFAULT 0,
  skipped_gone INTEGER NOT NULL DEFAULT 0,
  skipped_overwritten INTEGER NOT NULL DEFAULT 0,
  drift_dirs INTEGER NOT NULL DEFAULT 0,
  ledger_drift_dirs INTEGER NOT NULL DEFAULT 0,
  buckets TEXT,
  undo_deadline INTEGER,
  undo_state TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  purge_state TEXT NOT NULL DEFAULT 'none' CHECK (purge_state IN ('none', 'pending', 'done')),
  log_dir TEXT NOT NULL
, plan_digest TEXT, freed_bytes INTEGER);

CREATE TABLE deletion_bands (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  prefix TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  objects INTEGER NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  overwritten INTEGER NOT NULL DEFAULT 0,
  drift_new_objects INTEGER NOT NULL DEFAULT 0,
  undone_objects INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);

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

CREATE TABLE profiles (
  email       TEXT PRIMARY KEY,
  avatar      TEXT,

  avatar_src  TEXT,
  updated_at  INTEGER NOT NULL
, name TEXT);

CREATE TABLE "pyramid_multiscans" (
  dataset TEXT NOT NULL,
  tier TEXT NOT NULL,
  shard_dur TEXT NOT NULL,
  period_start INTEGER NOT NULL,
  period_end INTEGER NOT NULL,
  key TEXT NOT NULL,
  scans TEXT NOT NULL,
  encoder TEXT NOT NULL,
  digests TEXT,
  written_at INTEGER NOT NULL,
  PRIMARY KEY (dataset, key)
);

CREATE TABLE scan_runs (
  run_id       TEXT PRIMARY KEY,
  store        TEXT NOT NULL DEFAULT 'primary',
  scan         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  parent       TEXT,
  status       TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'nop')),
  failed_phase TEXT,
  error        TEXT,
  started_ts   INTEGER,
  finished_ts  INTEGER,
  job_name     TEXT,
  region       TEXT,
  machine      TEXT,
  spot         INTEGER,
  tasks        INTEGER,
  image        TEXT,
  cost_usd     REAL,
  gen          TEXT,
  source       TEXT NOT NULL CHECK (source IN ('job', 'backfill')),
  updated_ts   INTEGER NOT NULL
);

CREATE TABLE scan_run_dags (
  run_id  TEXT PRIMARY KEY REFERENCES scan_runs(run_id),
  engine  TEXT NOT NULL,
  dag     TEXT NOT NULL
);

CREATE TABLE scan_run_outputs (
  run_id  TEXT NOT NULL REFERENCES scan_runs(run_id),
  key     TEXT NOT NULL,
  uri     TEXT NOT NULL,
  bytes   INTEGER,
  objects INTEGER,
  rows    INTEGER,
  gen     TEXT,
  PRIMARY KEY (run_id, key)
);

CREATE TABLE scan_run_phases (
  run_id      TEXT NOT NULL REFERENCES scan_runs(run_id),
  phase       TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  started_ts  INTEGER,
  finished_ts INTEGER,
  status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('running', 'done', 'failed')),
  note        TEXT, job_name TEXT, machine TEXT, task TEXT,
  PRIMARY KEY (run_id, phase)
);

CREATE TABLE session_log (
  sid         TEXT PRIMARY KEY,
  email       TEXT,
  via         TEXT,
  build       TEXT,
  ua          TEXT,
  started_ms  INTEGER NOT NULL,
  last_ms     INTEGER NOT NULL,
  n_events    INTEGER NOT NULL,
  n_errors    INTEGER NOT NULL,
  n_batches   INTEGER NOT NULL,
  first_url   TEXT,
  received_ts INTEGER NOT NULL
);

CREATE TABLE session_log_batches (
  sid         TEXT NOT NULL,
  pid         TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  received_ts INTEGER NOT NULL,
  first_ms    INTEGER NOT NULL,
  last_ms     INTEGER NOT NULL,
  n           INTEGER NOT NULL,
  events      TEXT NOT NULL,
  PRIMARY KEY (sid, pid, seq)
);

CREATE TABLE session_log_switch (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  until_ms    INTEGER,
  who         TEXT NOT NULL,
  ts          INTEGER NOT NULL
);

CREATE TABLE stage_replies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  stager     TEXT NOT NULL,
  channel    TEXT NOT NULL,
  thread_ts  TEXT NOT NULL,
  slack_ts   TEXT,
  open       INTEGER,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL
);

CREATE TABLE stage_batches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  note       TEXT,
  created_by TEXT NOT NULL,
  created_ts INTEGER NOT NULL
, reply_id INTEGER REFERENCES stage_replies(id));

CREATE TABLE plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans(id),
  prefix   TEXT NOT NULL,
  note     TEXT,
  added_by TEXT NOT NULL,
  added_ts INTEGER NOT NULL, batch_id INTEGER REFERENCES stage_batches(id), as_of TEXT, kind TEXT CHECK (kind IS NULL OR kind = 'object'),
  PRIMARY KEY (plan_id, prefix)
);

CREATE UNIQUE INDEX access_log_dedupe ON access_log (event, session_sub, path, bucket) WHERE bucket IS NOT NULL;

CREATE INDEX access_log_grant ON access_log (grant_id, ts DESC);

CREATE INDEX access_log_ts ON access_log (ts DESC);

CREATE INDEX access_log_daily_day ON access_log_daily (day DESC);

CREATE INDEX access_log_daily_grant ON access_log_daily (grant_id, day DESC);

CREATE INDEX access_requests_created_at ON access_requests (created_at DESC);

CREATE INDEX access_requests_email ON access_requests (email, created_at DESC);

CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (email) WHERE status = 'pending';

CREATE INDEX access_requests_status ON access_requests (status, created_at DESC);

CREATE INDEX admin_edits_tbl ON admin_edits (tbl, ts);

CREATE INDEX idx_deletion_bands_prefix ON deletion_bands (prefix);

CREATE INDEX idx_footer_cache_lru ON footer_cache (state, last_access_day, date);

CREATE INDEX grants_created_at ON grants (created_at DESC);

CREATE INDEX grants_disabled_at ON grants (disabled_at);

CREATE INDEX idx_index_row_groups_depth ON index_row_groups (date, variant, gen, d_min, d_max);

CREATE INDEX idx_index_row_groups_user  ON index_row_groups (date, variant, gen, u_min, u_max);

CREATE INDEX idx_og_tokens_minted ON og_tokens (minted_ts DESC);

CREATE INDEX idx_og_tokens_token ON og_tokens (token);

CREATE INDEX pending_auth_email ON pending_auth (email, created_at);

CREATE INDEX pending_auth_ip ON pending_auth (ip_hash, created_at);

CREATE UNIQUE INDEX pending_auth_token ON pending_auth (token_hash);

CREATE INDEX idx_scan_runs_parent ON scan_runs (parent);

CREATE INDEX idx_scan_runs_scan ON scan_runs (store, scan);

CREATE INDEX idx_scan_runs_started ON scan_runs (store, started_ts);

CREATE INDEX idx_session_log_email ON session_log (email, last_ms);

CREATE INDEX idx_session_log_received ON session_log (received_ts);

CREATE INDEX idx_session_log_batches_received ON session_log_batches (received_ts);

CREATE UNIQUE INDEX stage_replies_open ON stage_replies (plan_id, stager) WHERE open = 1;
