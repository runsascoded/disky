-- `index_groups` was backfilled into `index_row_groups` (0020; gen 'legacy')
-- and nothing reads it. A one-statement DROP of the 8 GB table hit D1's
-- internal timeout, so it was emptied one (date, variant) at a time and then
-- dropped (2026-09-07); this migration records the end state and is a no-op.
DROP TABLE IF EXISTS index_groups;
