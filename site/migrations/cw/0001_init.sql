-- The whole D1 schema of oa-cw-s3-usage-db, as one baseline (squashed
-- 2026-09-28 from 20 incremental files, `0001_marks` … `0020_auth_single_name`;
-- the live database was rebaselined with @open-athena/auth's
-- `scripts/d1-rebaseline.mjs`, which proves the live schema matches this file
-- before rewriting wrangler's bookkeeping). The DDL is exactly what the 20
-- files built, so an existing database and a fresh one agree column for column.
--
-- Two families of tables live here:
--   · the @open-athena/auth tables (its `migrations/0001_init.sql`, copied so
--     this app owns one migration sequence): grants, access_log(_daily),
--     access_requests, profiles, pending_auth;
--   · the app's own: the sign-in allowlist and admin roster, the marks ledger
--     and plans, the path-index metadata + over-time manifest, deletion runs.
-- Sections say what each table is for, not how it got here.

------------------------------------------------------------------------------
-- @open-athena/auth: share links, sessions, audit, access requests, profiles
------------------------------------------------------------------------------

-- Share links. A grant is a revocable capability: its token (stored hashed)
-- redeems for a session that re-joins this row on every request, so revoking
-- the row ends every session it minted. `subject_json` is who the link is for
-- (`{ name?, email?, avatar? }`), `name` the admin's own label for it.
-- `disabled_at` stops new redemptions but leaves live sessions alone;
-- `expiry_ends_sessions` says whether `expires_at` also ends derived sessions;
-- `sessions_invalid_before` is stamped by a rotate that boots old sessions.
CREATE TABLE grants (
  id            TEXT PRIMARY KEY,       -- random, not autoincrement: doesn't leak counts
  token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256, base64url
  name          TEXT,                   -- admin-side label: "Bob Smith (donor)"
  note          TEXT,                   -- freeform: why this exists
  subject_json  TEXT,                   -- optional pre-loaded identity: {first,last,email,avatar}
  email         TEXT,                   -- if set: magic-link semantics (bind on redeem)
  scopes        TEXT NOT NULL,          -- space-separated
  max_redeems   INTEGER,                -- NULL = unlimited; counts sessions minted, not requests
  redeems       INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER,                -- NULL = never
  session_ttl   INTEGER,                -- seconds; NULL = inherit app default
  created_at    INTEGER NOT NULL,
  created_by    TEXT NOT NULL,
  revoked_at    INTEGER,
  first_used_at INTEGER,
  last_used_at  INTEGER
, disabled_at INTEGER, expiry_ends_sessions INTEGER NOT NULL DEFAULT 1, sessions_invalid_before INTEGER);
CREATE INDEX grants_created_at ON grants (created_at DESC);
CREATE INDEX grants_disabled_at ON grants (disabled_at);

-- Audit trail of sign-ins, redeems, denials and gated reads. `bucket` is the
-- time bucket the dedupe index folds repeated reads into.
CREATE TABLE access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  event       TEXT NOT NULL,   -- redeem | deny | revoke | request | view | signin | signout
  grant_id    TEXT,
  session_sub TEXT,            -- `e:<email>` | `g:<id>`
  path        TEXT,
  status      INTEGER,
  ip_hash     TEXT,
  ua          TEXT,
  country     TEXT,
  referer     TEXT,
  reason      TEXT,            -- deny detail: bad-token | revoked | expired | exhausted | not-allowed
  bucket      INTEGER          -- floor(ts/3600) for `view` rows; NULL otherwise
);
CREATE UNIQUE INDEX access_log_dedupe ON access_log (event, session_sub, path, bucket) WHERE bucket IS NOT NULL;
CREATE INDEX access_log_grant ON access_log (grant_id, ts DESC);
CREATE INDEX access_log_ts ON access_log (ts DESC);

-- Daily rollup of access_log, so per-link "who used it" views don't scan rows.
CREATE TABLE access_log_daily (
  day      INTEGER NOT NULL,   -- floor(ts / 86400)
  event    TEXT NOT NULL,
  grant_id TEXT,
  path     TEXT,
  country  TEXT,
  events   INTEGER NOT NULL,   -- rows collapsed into this bucket
  clients  INTEGER NOT NULL,   -- distinct ip_hash values within it
  PRIMARY KEY (day, event, grant_id, path, country)
);
CREATE INDEX access_log_daily_day ON access_log_daily (day DESC);
CREATE INDEX access_log_daily_grant ON access_log_daily (grant_id, day DESC);

-- "Request access" submissions from the sign-in wall; at most one pending per
-- email. A decision records who decided and, on approval, the grant minted.
CREATE TABLE access_requests (
  id         TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  name       TEXT,
  note       TEXT,
  created_at INTEGER NOT NULL,
  status     TEXT NOT NULL,   -- pending | approved | denied | auto
  decided_at INTEGER,
  decided_by TEXT,
  grant_id   TEXT,
  ip_hash    TEXT             -- HMAC, for rate-limiting; never a raw address
, subject_json TEXT);
CREATE INDEX access_requests_created_at ON access_requests (created_at DESC);
CREATE INDEX access_requests_email ON access_requests (email, created_at DESC);
CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (email) WHERE status = 'pending';
CREATE INDEX access_requests_status ON access_requests (status, created_at DESC);

-- A signed-in person's self-set name and face, keyed by email (the identity a
-- Google id_token and an emailed code share). `avatar` is a data: URI or an
-- asset:// ref, never a live remote URL.
CREATE TABLE profiles (
  email       TEXT PRIMARY KEY,
  avatar      TEXT,
  -- Provenance for re-resolve/debug: 'upload' | 'url' | 'github' | 'gravatar'.
  avatar_src  TEXT,
  updated_at  INTEGER NOT NULL
, name TEXT);

-- Pending emailed-code sign-ins: one row backs both the magic link and the
-- 6-digit code, both stored hashed, single-use and short-lived. `attempts`
-- caps code guesses; `ip_hash` backs per-source send rate limits.
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
CREATE INDEX pending_auth_email ON pending_auth (email, created_at);
CREATE INDEX pending_auth_ip ON pending_auth (ip_hash, created_at);
CREATE UNIQUE INDEX pending_auth_token ON pending_auth (token_hash);

------------------------------------------------------------------------------
-- Who may sign in, who administers, and the agent tokens
------------------------------------------------------------------------------

-- The sign-in allowlist: non-staff emails get the `cw` scope only while
-- listed here (`scopesFor` checks it on every request). Edited at
-- /admin/db/allowed_emails.
CREATE TABLE allowed_emails (
  email TEXT PRIMARY KEY,               -- lowercased
  note  TEXT,                           -- who this is / where they're from
  who   TEXT NOT NULL,                  -- admin who added the row
  ts    INTEGER NOT NULL                -- epoch seconds
);

CREATE TABLE admin_emails (
  email TEXT PRIMARY KEY,               -- lowercased; may mark sweep + dispatch runs
  note  TEXT,                           -- who this is
  who   TEXT NOT NULL,                  -- admin who added the row
  ts    INTEGER NOT NULL                -- epoch seconds
);

-- Append-only history of every write made through /api/db (any table).
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
CREATE INDEX admin_edits_tbl ON admin_edits (tbl, ts);

-- Personal agent tokens: the grant currently serving as a user's `cw`-scoped
-- bearer token (only its hash lives in `grants`; the raw token is shown once).
CREATE TABLE agent_tokens (
  email    TEXT PRIMARY KEY,          -- lower-cased owner email
  grant_id TEXT NOT NULL,             -- grants.id of the live token grant
  created  INTEGER NOT NULL           -- unix seconds, server-assigned
);

------------------------------------------------------------------------------
-- Keep / sweep decisions: marks and the mark log; plans
------------------------------------------------------------------------------

-- Pre-ledger marks, claims and their log: superseded by `actions` (their
-- contents were migrated into it) and kept read-only as history.
CREATE TABLE marks (
  prefix TEXT PRIMARY KEY,              -- s3://<bucket>/<path>/ (trailing slash)
  keep   TEXT NOT NULL CHECK (keep IN ('keep', 'keep_last_ckpt', 'sweep')),
  who    TEXT NOT NULL,                 -- CF Access identity (email) that set it
  scan   TEXT NOT NULL,                 -- scan id the actor was viewing
  ts     INTEGER NOT NULL,              -- epoch seconds
  note   TEXT
);

CREATE TABLE mark_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  prefix TEXT NOT NULL,
  keep   TEXT,                          -- NULL = mark removed (unmark)
  who    TEXT NOT NULL,
  scan   TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  note   TEXT
);
CREATE INDEX mark_log_prefix ON mark_log (prefix, ts);

-- A deletion plan as a first-class object (several can be drafted at once,
-- each with its own dry → real lifecycle).
CREATE TABLE plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_by TEXT NOT NULL,            -- CF Access identity that drafted it
  created_ts INTEGER NOT NULL,         -- epoch seconds
  closed_ts  INTEGER                   -- set when state → closed
);

-- The prefixes in a plan; `batch_id` links back to the gesture that staged them.
CREATE TABLE plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans(id),
  prefix   TEXT NOT NULL,             -- s3://<bucket>/<path>/ (trailing slash)
  note     TEXT,
  added_by TEXT NOT NULL,             -- CF Access identity that added it
  added_ts INTEGER NOT NULL,
  PRIMARY KEY (plan_id, prefix)
);

------------------------------------------------------------------------------
-- Path-index metadata (the parquet footers, decomposed so the edge reader can select row groups from D1) and the over-time group manifest
------------------------------------------------------------------------------

-- One row per (scan, index variant): the parquet schema, the tier's byte
-- floor (NULL = floor-free), and the pointer to the generation currently
-- serving (`gen`, `dir`), written last so two generations can coexist while a
-- new one lands. Variants: 'path' (drill), 'user', 'team'.
CREATE TABLE index_schema (
  date        TEXT NOT NULL,            -- scan id (SNAP_ID, e.g. 2026-09-16T0001)
  variant     TEXT NOT NULL DEFAULT 'path',  -- 'path' (floor-free) or 'coarse<E>'
  version     INTEGER NOT NULL,         -- parquet FileMetaData.version
  schema_json TEXT NOT NULL,            -- hyparquet SchemaElement[] (root + leaves)
  floor_bytes INTEGER,                  -- a coarse tier's absolute byte floor F; NULL = floor-free
  gen         TEXT,                     -- generation stamp of the synced file set
  dir         TEXT,                     -- bucket-relative dir holding this variant's parquet
  PRIMARY KEY (date, variant)
);

-- One row per row group of each generation: the stats the reader prunes on
-- (depth, path and usr ranges, max bytes) and the row-group metadata itself.
CREATE TABLE index_row_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL,
  gen       TEXT NOT NULL,
  rg        INTEGER NOT NULL,   -- row-group ordinal
  d_min     INTEGER NOT NULL,   -- depth stats (row-group pruning)
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,      -- path stats
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,   -- max descendant-inclusive bytes (threshold prune)
  u_min     TEXT,               -- usr range (a user-sorted variant's primary key; NULL here)
  u_max     TEXT,
  row_start INTEGER NOT NULL,   -- absolute row range of this group
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL,      -- compact [num_rows, codec, [[off, size, dict_off], …]]
  PRIMARY KEY (date, variant, gen, rg)
);
CREATE INDEX idx_index_row_groups_depth ON index_row_groups (date, variant, gen, d_min, d_max);
CREATE INDEX idx_index_row_groups_user  ON index_row_groups (date, variant, gen, u_min, u_max);

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

------------------------------------------------------------------------------
-- Deletion runs and executed sweeps
------------------------------------------------------------------------------

-- Executed sweeps: one row per executor invocation (dry or real), with what it
-- deleted, what drifted since the plan, the undo window, and the CAIOS purge
-- stage (always 'none' on this store). Object-level detail lives in `log_dir`.
CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY,           -- <plan>-<scan>/<utc compact ts>
  plan_id INTEGER NOT NULL REFERENCES plans(id),  -- the first-class plan executed
  manifest TEXT NOT NULL,            -- gs:// dir the object-level manifest was written to
  scan TEXT NOT NULL,
  head INTEGER NOT NULL,             -- mark_log head at pin (drift detection)
  exec_head INTEGER NOT NULL,        -- mark_log head at execution (re-verified)
  actor TEXT NOT NULL,               -- CF Access identity that dispatched
  mode TEXT NOT NULL CHECK (mode IN ('dry', 'real')),
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  deleted_bytes INTEGER NOT NULL DEFAULT 0,   -- would-delete bytes for dry runs
  deleted_objects INTEGER NOT NULL DEFAULT 0,
  skipped_gone INTEGER NOT NULL DEFAULT 0,     -- key already absent
  skipped_overwritten INTEGER NOT NULL DEFAULT 0, -- versionId/ETag drifted since scan
  drift_dirs INTEGER NOT NULL DEFAULT 0,      -- dirs skipped: new keys since the scan
  ledger_drift_dirs INTEGER NOT NULL DEFAULT 0, -- dirs dropped: newer marks
  buckets TEXT,                      -- bucket cut dispatched with (CSV); NULL = whole plan
  undo_deadline INTEGER,             -- finished_ts + hold window (real runs); undo before this
  undo_state TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  purge_state TEXT NOT NULL DEFAULT 'none' CHECK (purge_state IN ('none', 'pending', 'done')),
  log_dir TEXT NOT NULL              -- gs:// dir holding {would-delete,deleted}/ parquets + summary
);

-- A run's deletions aggregated per covering band prefix — the queryable
-- per-path unit of deletion history.
CREATE TABLE deletion_bands (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  prefix TEXT NOT NULL,              -- s3://<bucket>/<dir>/ band the deletions fell under
  bytes INTEGER NOT NULL,
  objects INTEGER NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  overwritten INTEGER NOT NULL DEFAULT 0,
  drift_new_objects INTEGER NOT NULL DEFAULT 0,
  undone_objects INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);
CREATE INDEX idx_deletion_bands_prefix ON deletion_bands (prefix);
