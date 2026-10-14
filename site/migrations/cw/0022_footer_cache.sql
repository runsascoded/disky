-- The footer LRU (specs/done/footer-lru.md): which (scan, variant, generation)
-- footer sets `index_row_groups` holds, in what state, and when each was last
-- read. A reader trusts D1's rows for an entry only while it is `hot`; an
-- entry without a row here is a sync's complete set from before this table
-- (or a variant the LRU never manages) and is trusted as before.
--
--   filling   a fill (or a sync) is inserting the rows; readers serve the cold
--             `.groups.parquet`. `started_ts` stamps the claim: a stale one is
--             taken over by the next miss.
--   hot       every row landed (the fill checked the count); readers use D1.
--   evicting  demoted by `evictFooters` / `dt-cloud footer-evict` /
--             `index-gc -r`; readers serve cold, and the rows are deleted
--             once the demotion is older than a handle's lifetime, then this row.
--
-- `variant` is as `index_row_groups` names it (a secondary store's
-- `<store>:<variant>`), so the PK mirrors that table's (date, variant, gen).
CREATE TABLE footer_cache (
  date            TEXT NOT NULL,
  variant         TEXT NOT NULL,
  gen             TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('filling', 'hot', 'evicting')),
  rows            INTEGER NOT NULL DEFAULT 0,  -- index_row_groups rows the entry holds
  bytes           INTEGER NOT NULL DEFAULT 0,  -- their text columns' lengths + 64 per row
  started_ts      INTEGER NOT NULL,            -- epoch s: the fill's claim, or the demotion
  filled_ts       INTEGER,                     -- epoch s the last batch landed
  last_access_day INTEGER NOT NULL DEFAULT 0,  -- epoch day of the newest read (written ≤ once a day)
  PRIMARY KEY (date, variant, gen)
);
CREATE INDEX idx_footer_cache_lru ON footer_cache (state, last_access_day, date);
