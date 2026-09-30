-- Mark & sweep (specs/mark-sweep-ui.md): human keep/delete marks over GCS
-- prefixes, ahead of the post-2026-08-28 sweep.
--
-- Only non-default marks are stored — `delete` is the default (absence of a
-- mark), but explicit `delete` rows are allowed so a child can be carved out
-- of a `keep`ed parent (resolution is deepest-mark-wins, same semantics as
-- the attribution rules). `mark_log` is append-only history: every change,
-- including un-marks, so the sweep's provenance survives later edits.

CREATE TABLE marks (
  prefix TEXT PRIMARY KEY,              -- gs://bucket/path/ (trailing slash)
  action TEXT NOT NULL CHECK (action IN ('keep', 'keep_last_ckpt', 'delete')),
  who    TEXT NOT NULL,                 -- email or grant name
  ts     INTEGER NOT NULL,              -- epoch seconds
  note   TEXT
);

CREATE TABLE mark_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  prefix TEXT NOT NULL,
  action TEXT,                          -- NULL = mark removed
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  note   TEXT
);
CREATE INDEX mark_log_prefix ON mark_log (prefix, ts);

-- Lost & found: claiming an unattributed prefix as yours (then mark it like
-- your own). Claim ≠ mark: a claim without marks still defaults to delete.
CREATE TABLE claims (
  prefix TEXT PRIMARY KEY,
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL
);
