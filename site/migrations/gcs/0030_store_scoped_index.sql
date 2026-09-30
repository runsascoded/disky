-- Store-scoped index tables (specs/multi-store.md phase 1): a deployment may
-- serve secondary stores beside its primary (e.g. a `/meta` self-scan), each
-- with its own scans. Every index row now names its store; the primary's rows
-- are `'primary'`, a fixed sentinel rather than the deploy's `STORE` key, the
-- same in both lineages (cw's `0006_store_scoped_index.sql` is this file).
--
-- Only a secondary store needs this migration: the primary's reads and writes
-- never name `store` (their SQL predates it; its rows get the default), so
-- they run the same on either schema. A secondary store's rows also carry a
-- namespaced variant (`<store>:<variant>`), which no primary query asks for,
-- and its queries say `store = ?`, which fails until this is applied.
--
-- `index_schema` (the per-scan pointer: one small row per (scan, variant)) is
-- rebuilt with `store` leading its primary key. Nothing REFERENCES
-- `index_schema`, so the DROP + RENAME can't trip D1's foreign-key
-- enforcement.
--
-- `index_row_groups` is NOT rebuilt: it is the multi-GB table (a DROP COLUMN
-- on it ran past D1's statement limit, 0019), so `store` is added in place (a
-- constant default rewrites no rows) and its PK stays (date, variant, gen,
-- rg), which the namespaced variant keeps disjoint across stores. The
-- writer's gc / retention passes filter on the column.
--
-- `pyramid_multiscans` (the over-time group manifest) is untouched: pyrmts
-- owns its DDL and read query (`CREATE TABLE IF NOT EXISTS` at first sync —
-- this lineage never created it), and its `dataset` column already namespaces
-- it (a secondary store's groups are dataset `<store>:over-time`).
CREATE TABLE index_schema_new (
  store       TEXT NOT NULL DEFAULT 'primary',  -- 'primary' or a `STORES_JSON` key
  date        TEXT NOT NULL,            -- snapshot date / scan id the index belongs to
  variant     TEXT NOT NULL DEFAULT 'path',
  version     INTEGER NOT NULL,         -- parquet FileMetaData.version
  schema_json TEXT NOT NULL,            -- hyparquet SchemaElement[] (root + leaves)
  floor_bytes INTEGER,                  -- a coarse tier's absolute byte floor F; NULL = floor-free
  gen         TEXT,                     -- generation stamp of the synced file set
  dir         TEXT,                     -- bucket-relative dir holding this variant's parquet
  PRIMARY KEY (store, date, variant)
);
INSERT INTO index_schema_new (store, date, variant, version, schema_json, floor_bytes, gen, dir)
  SELECT 'primary', date, variant, version, schema_json, floor_bytes, gen, dir FROM index_schema;
DROP TABLE index_schema;
ALTER TABLE index_schema_new RENAME TO index_schema;

ALTER TABLE index_row_groups ADD COLUMN store TEXT NOT NULL DEFAULT 'primary';
