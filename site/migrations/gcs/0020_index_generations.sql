-- Index generations (specs/view-serving.md, "Index rewrite vs D1 footer"): a
-- REPROC used to overwrite `listing/<date>/path-index*.parquet` in place while
-- D1 still held the previous file's row-group offsets — reads in the ~10 min
-- window decoded garbage, and a cancelled job left it that way. Now every run
-- writes its tiers under a fresh `listing/<date>/index/<gen>/`, never
-- overwriting a file D1 points at, and the row groups carry that `gen` so two
-- generations coexist while the new one lands. The `index_schema` row is the
-- pointer: (gen, dir) per (date, variant), written last as one statement — the
-- reader sees the old complete set or the new one, never a mix.
--
-- `index_row_groups` replaces `index_groups` (whose PK (date, variant, rg)
-- can't hold two generations). The old table is backfilled into this one
-- (gen 'legacy', dir 'listing/<date>') and dropped by a later migration —
-- a DROP COLUMN or rebuild of the 8 GB table is what D1 can't finish in one
-- statement (0019), a DROP TABLE frees pages without rewriting them.
ALTER TABLE index_schema ADD COLUMN gen TEXT;   -- generation stamp of the synced file set
ALTER TABLE index_schema ADD COLUMN dir TEXT;   -- bucket-relative dir holding this variant's parquet

CREATE TABLE index_row_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL,
  gen       TEXT NOT NULL,
  rg        INTEGER NOT NULL,
  d_min     INTEGER NOT NULL,   -- depth stats (secondary within a single key)
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,      -- path stats
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,   -- max bytes (threshold prune; = lens bytes in by-user)
  u_min     TEXT,               -- usr range (by-user primary key; NULL elsewhere)
  u_max     TEXT,
  row_start INTEGER NOT NULL,
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL,      -- compact [num_rows, codec, [[off, size, dict_off], …]]
  PRIMARY KEY (date, variant, gen, rg)
);
CREATE INDEX idx_index_row_groups_depth ON index_row_groups (date, variant, gen, d_min, d_max);
CREATE INDEX idx_index_row_groups_user  ON index_row_groups (date, variant, gen, u_min, u_max);
