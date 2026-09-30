-- The `keep_last_ckpt` mark kind is retired: "keep the newest checkpoint,
-- sweep the rest" is expressed as plain keep/sweep marks over the API now
-- (an agent that knows the run layout marks the exact prefixes). Live marks of
-- that kind are dropped — the prefix goes back to unmarked, the review backlog,
-- never to sweep — and the CHECK on `marks.keep` is rebuilt without it (SQLite
-- can't alter a constraint in place). `mark_log` is append-only provenance
-- that nothing resolves state from, so its history rows stay.
DELETE FROM marks WHERE keep = 'keep_last_ckpt';

CREATE TABLE marks_new (
  prefix TEXT PRIMARY KEY,              -- s3://<bucket>/<path>/ (trailing slash)
  keep   TEXT NOT NULL CHECK (keep IN ('keep', 'sweep')),
  who    TEXT NOT NULL,                 -- CF Access identity (email) that set it
  scan   TEXT NOT NULL,                 -- scan id the actor was viewing
  ts     INTEGER NOT NULL,              -- epoch seconds
  note   TEXT
);
INSERT INTO marks_new (prefix, keep, who, scan, ts, note)
  SELECT prefix, keep, who, scan, ts, note FROM marks;
DROP TABLE marks;
ALTER TABLE marks_new RENAME TO marks;
