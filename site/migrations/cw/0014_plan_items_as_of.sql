-- A staged item is "as of" a scan: the scan date (`index_schema.date`, e.g.
-- `2026-10-07`) whose listing the stager saw. The executor deletes an object
-- only if it is in BOTH the dispatch scan and the item's `as_of` scan with the
-- same identity (GCS generation; else `created` / mtime), so nothing written
-- after the staging is swept with it. `POST /api/plans/stage` sets it
-- (default: the latest scan); a re-stage keeps the first one. NULL = staged
-- before this column (the dispatch scan stands in). Additive: the prod D1
-- takes it in place.
ALTER TABLE plan_items ADD COLUMN as_of TEXT;
