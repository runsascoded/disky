-- Actions ledger (specs/actions-ledger.md): attribution + keep/sweep as one
-- append-only WAL. Raw `actions` (one audit row per user gesture) + per-axis
-- expanded prefix tables for resolution (most-recent-wins per axis).
-- Supersedes marks/claims/mark_log — those stay in place, read-only, for
-- history; their contents migrate below.

CREATE TABLE actions (
  id         INTEGER PRIMARY KEY,
  actor      TEXT NOT NULL,             -- email of the acting identity
  ts         INTEGER NOT NULL,          -- unix seconds, server-assigned
  scan       TEXT NOT NULL,             -- scan id the actor was viewing
  pattern    TEXT NOT NULL,             -- prefix (regex patterns arrive later)
  set_owner  INTEGER NOT NULL DEFAULT 0,
  owner      TEXT,                      -- user id; NULL with set_owner=1 = clear
  set_keep   INTEGER NOT NULL DEFAULT 0,
  keep       TEXT,                      -- 'keep'|'sweep'|'keep_last_ckpt'; NULL with set_keep=1 = unmark
  memo       TEXT,
  CHECK (set_owner OR set_keep)
);
CREATE INDEX idx_actions_actor ON actions (actor, ts);

CREATE TABLE owner_prefixes (
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  owner      TEXT,                      -- NULL = cleared
  ts         INTEGER NOT NULL,          -- denormalized actions.ts (immutable)
  tombstoned TEXT,                      -- scan id the prefix vanished; NULL = live
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_owner_prefix_ts ON owner_prefixes (prefix, ts DESC);

CREATE TABLE keep_prefixes (
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  keep       TEXT,
  ts         INTEGER NOT NULL,
  tombstoned TEXT,
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_keep_prefix_ts ON keep_prefixes (prefix, ts DESC);

-- Migrate the full mark history (mark_log, not just the folded `marks`):
-- every log row becomes a keep-axis action (NULL action = unmark = clear),
-- so the recency fold reproduces the current state and keeps provenance.
INSERT INTO actions (actor, ts, scan, pattern, set_keep, keep, memo)
  SELECT who, ts, 'pre-ledger', prefix, 1,
         CASE action WHEN 'delete' THEN 'sweep' ELSE action END, note
  FROM mark_log ORDER BY ts, id;

-- Claims become owner-axis self-assignments (canonical user id where mapped).
INSERT INTO actions (actor, ts, scan, pattern, set_owner, owner)
  SELECT c.who, c.ts, 'pre-ledger', c.prefix, 1, COALESCE(ue.user, c.who)
  FROM claims c LEFT JOIN user_emails ue ON ue.email = lower(c.who);

INSERT INTO keep_prefixes (action_id, prefix, keep, ts)
  SELECT id, pattern, keep, ts FROM actions WHERE set_keep = 1;
INSERT INTO owner_prefixes (action_id, prefix, owner, ts)
  SELECT id, pattern, owner, ts FROM actions WHERE set_owner = 1;
