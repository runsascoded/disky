-- Stage batches — the deletion *action* as a first-class row. One trash gesture
-- stages N prefixes with one shared memo, so the note is a fact about the
-- decision, not copied onto every path. A batch belongs to the plan its items
-- land in (the shared open "Staged" plan today); `plan_items.batch_id` is the
-- 1:many link (one batch → many items). Pre-batch stagings keep batch_id NULL.
--
-- Shared schema (0024's seam 3): cw-s3 mirrors this migration; `IF NOT EXISTS`
-- keeps it a no-op wherever the table already exists.

CREATE TABLE IF NOT EXISTS stage_batches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  note       TEXT,                    -- the one memo for this gesture
  created_by TEXT NOT NULL,           -- the identity that trashed
  created_ts INTEGER NOT NULL         -- epoch seconds
);

ALTER TABLE plan_items ADD COLUMN batch_id INTEGER REFERENCES stage_batches(id);
