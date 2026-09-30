-- Ownership is a person or nobody (the group facet was excised 2026-09-06):
-- drop the team-range prune index. The `t_min` / `t_max` columns stay, NULL
-- and unread: SQLite's DROP COLUMN rewrites the whole table, and on this
-- 2M-row / 8 GB table that ran past D1's statement limit (tried 2026-09-06).
-- `rg_json` is now the compact `[num_rows, codec, [[data_page_offset,
-- total_compressed_size, dictionary_page_offset|0], …]]` array
-- (cloud/src/dt_cloud/index_footer.py); existing verbose rows are
-- rewritten in place by `dt-cloud index-compact` (the reader revives both
-- forms until that has run everywhere).
DROP INDEX IF EXISTS idx_index_groups_team;
