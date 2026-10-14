-- Coalesced stage replies in a staged plan's Slack thread
-- (`_lib/stagedSlack.ts`). A stager's successive stage requests within 15
-- minutes of their reply's last update edit that one reply in place
-- (`chat.update`, which notifies no one) instead of posting a new one each.
-- A row is one such reply: `open = 1` while it still takes its stager's
-- stages (at most one per plan and stager: the partial unique index is the
-- race-safe claim, like `plans.slack_ts IS NULL` for the parent); `slack_ts`
-- stays NULL while its claimer is posting it. `stage_batches.reply_id` = the
-- reply announcing the batch (NULL: announced before this, or never).
-- Additive only: a new table, an index and a nullable column (D1 enforces
-- foreign keys; no referenced table is rebuilt).
CREATE TABLE IF NOT EXISTS stage_replies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  stager     TEXT NOT NULL,             -- lowercased identity of the batches' stager
  channel    TEXT NOT NULL,
  thread_ts  TEXT NOT NULL,             -- the parent it replies under
  slack_ts   TEXT,                      -- the reply; NULL while being posted
  open       INTEGER,                   -- 1 = taking this stager's stages; NULL = closed
  created_ts INTEGER NOT NULL,          -- epoch seconds
  updated_ts INTEGER NOT NULL           -- the last stage it took
);
CREATE UNIQUE INDEX IF NOT EXISTS stage_replies_open ON stage_replies (plan_id, stager) WHERE open = 1;

ALTER TABLE stage_batches ADD COLUMN reply_id INTEGER REFERENCES stage_replies(id);
