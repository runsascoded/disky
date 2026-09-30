-- The `keep_last_ckpt` mark kind is retired: "keep the newest checkpoint,
-- sweep the rest" is expressed as plain keep/sweep marks over the API now.
--
-- The live ledger resolves state from `keep_prefixes` (joined to `actions`
-- for provenance), so dropping the kind's rows there un-marks those prefixes —
-- back to undecided, the review backlog. `actions` is the append-only audit
-- WAL and keeps its rows (nothing reads `actions.keep` except through that
-- join); the retired kind is never re-expanded from it.
DELETE FROM keep_prefixes WHERE keep = 'keep_last_ckpt';

-- The pre-ledger `marks` table (0007; read-only history since 0010) carries
-- the kind in its CHECK: drop the rows and rebuild the constraint without it
-- (SQLite can't alter a constraint in place). `mark_log` stays as-is.
DELETE FROM marks WHERE action = 'keep_last_ckpt';

CREATE TABLE marks_new (
  prefix TEXT PRIMARY KEY,              -- gs://bucket/path/ (trailing slash)
  action TEXT NOT NULL CHECK (action IN ('keep', 'delete')),
  who    TEXT NOT NULL,                 -- email or grant name
  ts     INTEGER NOT NULL,              -- epoch seconds
  note   TEXT
);
INSERT INTO marks_new (prefix, action, who, ts, note)
  SELECT prefix, action, who, ts, note FROM marks;
DROP TABLE marks;
ALTER TABLE marks_new RENAME TO marks;
