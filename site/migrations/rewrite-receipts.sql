-- thrds bulk-rewrite receipts (`d1Receipts`, `@open-athena/thrds/d1`'s `rewriteReceiptsSchema()`): one row per outcome of
-- a real apply from `/admin/slack`'s review of a digest re-apply plan (`/api/digest-reapply/<job>/apply`), keyed by the
-- plan (`plan` = the run's job id), so a later batch or a re-run skips what's `done` / `already`
-- (specs/done/digest-rewrite-review.md).
CREATE TABLE IF NOT EXISTS thrds_rewrite_receipts (
  plan TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  new_ts TEXT,
  error TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS thrds_rewrite_receipts_plan ON thrds_rewrite_receipts (plan, status);
