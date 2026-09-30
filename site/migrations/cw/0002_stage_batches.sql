-- Stage batches — the deletion *action* as a first-class row (the opt-in
-- trash model, specs/staged-delete.md; gcs lineage 0026 verbatim; after the squashed `0001_init` baseline). One trash
-- gesture stages N prefixes with one shared memo, so the note is a fact about
-- the decision, not copied onto every path. A batch belongs to the plan its
-- items land in (the shared open "Staged" plan); `plan_items.batch_id` is the
-- 1:many link. Pre-batch items keep batch_id NULL. Required before a cw
-- deployment turns `STAGING` on.

CREATE TABLE IF NOT EXISTS stage_batches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  note       TEXT,                    -- the one memo for this gesture
  created_by TEXT NOT NULL,           -- the identity that trashed
  created_ts INTEGER NOT NULL         -- epoch seconds
);

ALTER TABLE plan_items ADD COLUMN batch_id INTEGER REFERENCES stage_batches(id);
