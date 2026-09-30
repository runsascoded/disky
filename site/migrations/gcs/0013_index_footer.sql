-- The path-index parquet footer, decomposed into D1 so the Cloudflare reader
-- builds a subset FileMetaData for a prefix query without parsing the whole
-- ~5 MB thrift footer on a cold isolate — the parse cost scales with row-group
-- count and blew the Worker CPU budget at 8k-row groups (1102). With the footer
-- here, row-group count no longer touches cold-start cost, so groups can shrink
-- freely (specs/path-agnostic-serving.md §2.1). Populated per scan by
-- `dt-cloud index-sync`; old scans are dead weight, prune at leisure.

CREATE TABLE index_schema (
  date       TEXT PRIMARY KEY,          -- snapshot date the index belongs to
  version    INTEGER NOT NULL,          -- parquet FileMetaData.version
  schema_json TEXT NOT NULL             -- hyparquet SchemaElement[] (root + leaves)
);

CREATE TABLE index_groups (
  date      TEXT NOT NULL,
  rg        INTEGER NOT NULL,           -- row-group ordinal
  d_min     INTEGER NOT NULL,           -- depth stats (row-group pruning)
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,              -- path stats
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,           -- max descendant-inclusive bytes (threshold prune)
  row_start INTEGER NOT NULL,           -- absolute row range of this group
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL,              -- stripped RowGroup metadata (reads only)
  PRIMARY KEY (date, rg)
);
-- Pruning is a range scan on (date, depth); the path filter is applied after.
CREATE INDEX idx_index_groups_depth ON index_groups (date, d_min, d_max);
