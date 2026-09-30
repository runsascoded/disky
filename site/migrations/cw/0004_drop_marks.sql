-- The keep/sweep mark system is retired (specs/marks-demolition.md): the
-- opt-in staged-deletion model (`plans` / `plan_items` / `stage_batches`,
-- 0001_init + 0002) is the only delete path, and nothing reads `marks` or
-- `mark_log` any more (`/api/marks`, `/api/plan-marks` and the site's mark UI
-- are gone). Both tables were created by 0001_init.sql; `marks_new` is the
-- rebuild scratch name 0003 used, dropped here in case an apply was cut off
-- between its DROP and RENAME.
--
-- BEFORE APPLYING ON PROD (Ryan gates the apply): the rows are exported —
--   npx wrangler d1 export oa-cw-s3-usage-db --remote --table marks --table mark_log \
--     --output tmp/marks-backup-2026-09-28.sql
-- and the file uploaded to the store's bucket under `backups/`. The
-- `d1_migrations` rows of earlier migrations stay as history.
DROP TABLE IF EXISTS marks;
DROP TABLE IF EXISTS mark_log;
DROP TABLE IF EXISTS marks_new;
