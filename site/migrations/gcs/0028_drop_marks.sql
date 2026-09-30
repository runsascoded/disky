-- The keep/sweep mark system is retired (specs/marks-demolition.md): the
-- opt-in staged-deletion model (`plans` / `plan_items` / `stage_batches`,
-- 0024–0026) is the only delete path, and the actions ledger keeps its owner
-- axis only. Composes after 0027 (which rebuilt the pre-ledger `marks` with a
-- keep/delete CHECK and pruned `keep_prefixes`): every mark table goes, the
-- keep-only actions go, and `actions` is rebuilt without its keep columns.
--
-- BEFORE APPLYING ON PROD (Ryan gates the apply): the rows are exported —
--   npx wrangler d1 export oa-gcs-usage-db --remote --table actions --table keep_prefixes \
--     --table mark_totals --table sweep_approvals --table marks --table mark_log \
--     --output tmp/marks-backup-2026-09-28.sql
-- and the file uploaded to the store's bucket under `backups/`
-- (gs://oa-gcs-usage-dvx/backups/marks-backup-2026-09-28.sql.gz is the one
-- taken 2026-09-28). The `d1_migrations` rows of earlier migrations stay as
-- history.
--
-- `owner_prefixes` references `actions (id)`: the drop / rename below runs
-- inside the migration's transaction, so the constraint is checked once
-- `actions` exists again (with the same ids) rather than at the DROP.
-- `actions` is NOT rebuilt: `owner_prefixes.action_id` references it, and
-- dropping a parent table runs an implicit DELETE against every child row —
-- `PRAGMA defer_foreign_keys` does not forgive that once the rebuilt table is
-- renamed into place, so D1 rejected the rebuild on prod (2026-09-28, code
-- 7500, rolled back). The keep columns (`set_keep`, `keep`) stay as unused
-- defaults: the only writer (`api/actions.ts`) inserts `set_owner = 1`, which
-- satisfies the table's `CHECK (set_owner OR set_keep)`.
DROP TABLE IF EXISTS marks;
DROP TABLE IF EXISTS mark_log;
DROP TABLE IF EXISTS marks_new;
DROP TABLE IF EXISTS keep_prefixes;
DROP TABLE IF EXISTS mark_totals;
DROP TABLE IF EXISTS sweep_approvals;
-- Keep-only actions have no owner rows pointing at them (owner_prefixes only
-- expands owner actions), so this is a plain delete.
DELETE FROM actions WHERE set_keep = 1 AND set_owner = 0;
CREATE INDEX IF NOT EXISTS idx_actions_actor ON actions (actor, ts);
CREATE TABLE owner_totals (
  scan        TEXT NOT NULL,             -- snapshot date the totals are priced on
  head        INTEGER NOT NULL,          -- max(actions.id) the fold included
  body        TEXT NOT NULL,             -- JSON response body
  claims      TEXT NOT NULL,             -- JSON: the body's `claims` alone
  computed_ts INTEGER NOT NULL,
  ms          INTEGER,                   -- compute wall time
  PRIMARY KEY (scan, head)
);
