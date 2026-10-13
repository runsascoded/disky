# Footers store (FS): one metadata store for many parquets

**Status:** design draft v1, 2026-10-10. This is research and design only: no code, deploys, bucket writes or D1 writes. Ryan decides the homes (§6) and the harmonization order (§7).

## 0. The pattern (Ryan's framing)

A dataset is **many parquet files on object storage**. Next to them sits **one footers store** that describes **all of them**. It is one FS for many parquets, not a sidecar per file.

From the FS alone, a client can plan the exact row groups and column chunks a query needs, then pull them with byte-range reads. It never fetches any data file's footer.

The client API Ryan has in mind: "give me a client I can use to query any of multiple parquets", built from an FS spec.
- **D1:** the FS is a set of D1 tables.
- **D1 + R2:** a "parquet of footers" on R2. This is the spill and fallback when D1 is too small (D1 caps at 10 GB per database).
- **Maybe others:** for example, a DuckLake catalog export (§3.4).

The client efficiently pulls row groups and columns from any parquet the FS describes. **Apps do their own batching** of nearby ranges to cut sequential round trips; the client provides the primitives for it (§4.4).

**Languages:** writes are Python (batch jobs, pyarrow) and reads are TypeScript (Cloudflare Workers, hyparquet). The design is fitted to that split. There is no dual-language implementation of either side; the shared contract is the format (§2) plus test fixtures.

**Ryan's question:** why don't his stores already use one pattern? §1 shows that they mostly *do* use one pattern, hand-rolled six times with six schemas. §7 says which of them should converge and which legitimately differ.

## 1. Inventory: what exists today

### 1.1 Summary

| Store | Repo | Where the "footers" live | Granularity | Pruning stats | Chunk offsets | Footer read on hot path? |
|---|---|---|---|---|---|---|
| Per-scan path store, hot | disky | D1 `index_schema` + `index_row_groups` | per RG | `d,p,b_max,u` (fixed columns) | compact `rg_json` | no |
| Per-scan path store, cold | disky | `<tier>.groups.parquet` beside each tier (R2/GCS) | per RG (512 per footer group) | `d,p,b_min,b_max,u` | `rg_json` | the small `.groups.parquet` footer only |
| Per-scan path store, legacy | disky | `<tier>.groups.json` blob | per RG | same | `rg_json` | the whole blob (86 MB on gcs) |
| Interval store | disky | `served/<sort>.groups.parquet` per sort and run, R2 | per RG | `+ seg, vf, vt` | `rg_json` | the `.groups.parquet` footer, compacted and colo-cached |
| Static name index | disky | each shard's own footer, walked by a Thrift cutter; plus `sidecar.parquet`, `catalog/index.parquet`, `*-index.top.parquet` | per RG | `s_min/s_max`, `q_min/q_max`, ... | `chunks` | yes, a suffix GET of the shard footer (colo-cached) |
| awair pyramid shards | awair | D1 `pyramid_shards.footer_bytes` (raw 64 KiB tail) | per file (opaque bytes) | none (hyparquet parses the tail) | none | served from D1 in place of R2 |
| ctbk GBFS shards | ctbk | D1 `rg_manifest` (+ `_schema`, `_fills`) | per RG | `cell`, `dt` | `chunk_meta` (verbose JSON, ~1 KB/RG) | no (falls back to the footer on a miss) |
| pyrmts walkdiff | pyrmts | caller-supplied `RowGroupIndex` | per RG | `depth,path` | `RowGroup` objects | no, when supplied |
| crashes cells-api | hccs/crashes | isolate LRU of parsed `FileMetaData` | per file | via hyparquet | via hyparquet | once per isolate |

### 1.2 disky: per-scan path store (D1 + cold parquet + legacy blob)

- **DDL.** `index_schema` is the per-(scan, variant) pointer: `version`, `schema_json`, `floor_bytes`, `gen`, `dir` ([`0001_init.sql`]:228-237; rebuilt with a leading `store` column in [`0006_store_scoped_index.sql`]:45-58). `index_row_groups` holds one row per RG: `d_min/d_max, p_min/p_max, b_max, u_min/u_max, row_start, row_end, rg_json`, with PK `(date, variant, gen, rg)` and indexes on `(…, d_min, d_max)` and `(…, u_min, u_max)` ([`0001_init.sql`]:241-259). `store` was added in place because the table is multi-GB ([`0006_store_scoped_index.sql`]:19-25, :60).
- **`rg_json`** is the compact per-RG chunk record `[num_rows, codec, [[data_page_offset, total_compressed_size, dictionary_page_offset|0], …]]`, one triple per leaf in schema order, ~250 B ([`index_footer.py`]:9-16). `reviveRowGroup` rebuilds a hyparquet `RowGroup` from it plus the schema ([`index.ts`]:1266-1293). This is the key trick shared by every disky store: a **synthetic `FileMetaData`** of the selected RGs only, passed to `parquetReadObjects` ([`index.ts`]:1363-1375).
- **Writer.** `extract` reads a footer into a schema plus RG rows ([`index_footer.py`]:157). `_group_rows` takes stats from the `depth`, `path`, size (`tot|size|b`) and `usr` columns (:88-137). `sync_d1` writes RG rows first and flips the `index_schema` pointer last (:389-494). D1 writes go through the HTTP API in ≤64 KB multi-row INSERTs (`INSERT_BYTES` :320, `_pack` :642, `_d1_query` :323), with retries. `gc_d1` (:497) and `retire_d1` (:570-626) delete unpointed generations and age out RGs past the newest N scans, the latter only when the cold `.groups.parquet` exists. CLI: `dt-cloud index-sync` / `index-gc` ([`cli.py`]:881-1007). gcs runs `-r 30`; cw runs unbounded ([`index-landscape.md`]:75-76, :281).
- **Cold tier format.** The engine owns it in [`groups.py`]: `FOOTER_COLS` (:79), 512-row groups (:77), stats only on the bound columns (:81), and kv metadata `groups_v, version, schema, floor_bytes`, so the file is self-describing (:258-275). `disk-tree tiers -g` writes it ([`tiers.py`]:40).
- **Reader** ([`index.ts`]):
  - `openIndex` (:398-428) reads the pointer (`schemaRow` :322), then probes whether D1 still has RGs for that gen (:416). If yes: `d1` mode. If not: `pq` mode over `.groups.parquet` (`openFooter` :959). Failing that: `blob` mode (`openBlob` :849).
  - Planning: `selectSpans1` (:1786) compiles rects into SQL over `index_row_groups`, or evaluates `groupMatches` (:1742) over cold footer groups (`pqGroups` :1157). `fetchGroupJson` (:1828) fetches `rg_json` only for the chosen RGs. `planSubtree` ([`view.ts`]:402) plans `path` and `bysize` before decoding anything, and keeps the cheaper read.
  - Reading: `chunkSpan` (:1295) gives one range per RG's projected chunks. `planRuns` (:1325; `RUN_GAP` 512 KiB, `RUN_BYTES` 2 MiB, `RUN_READS` 6) merges neighbouring spans. `readGroupsCached` (:1614) adds the decoded-RG LRU and in-flight sharing.
- **Size.** ~766-830 B per D1 row including indexes, ~95K RGs per sort at 8K rows: **~218 MB per scan** for the sort set, so `INDEX_RETAIN=30` ≈ 6.5 GB of the 10 GB cap ([`path-store.md`]:93, :104-106, :226; [`interval-store.md`]:29). The cold tier costs ~170-190 B per row ([`path-store.md`]:102, :106).

### 1.3 disky: interval store (R2 only, parquet of footers)

- **Writer.** `write_served` writes `served/<sort>.parquet` and `<sort>.groups.parquet` ([`interval_store.py`]:458-545). `GROUPS_SCHEMA` (:398-416) is the cold-tier columns plus `seg, vf_min/max, vt_min/max`. Bounds are **exact, from the rows** (`_bounds` :443), never from possibly-truncated footer stats. Each per-scan run publishes its own sorts plus `.groups.parquet` ([`interval_append.py`]:225-292).
- **Reader.** `ivFooter` / `ivRunFooter` ([`index.ts`]:487, :665) open each sort's `.groups.parquet` once per isolate per generation. Three colo-cache layers sit in front:
  - the **compact footer** `CompactFooter` (:1031-1050): the footer as footer-group byte ranges plus bounds, ~40 KB in place of ~0.5 MB of Thrift;
  - the **footer-group doc** `ivFooterDoc` (:1064-1096): a footer group's columns, pre-decoded;
  - the **decoded row group** `GroupDoc` (:1395-1440): gzipped columnar JSON of a data RG, shared by every scan because an interval RG holds every version.
- **No D1.** A D1 group index was considered and not proposed: ~218 MB per sort pair, and it helps only a cold colo ([`interval-store.md`]:430).
- **Sizes.** ~141K RGs (~21 MB of footer rows) per generation; the `.groups.parquet` footer is 527 KB with 276 footer groups ([`interval-store.md`]:118-121).

### 1.4 disky: static name index (are these FS uses?)

The data files are **app-level secondary indexes that happen to be parquet**. The key schemes are app logic: suffix keys `s` in `sx/s####.parquet`, `q` cells in `catalog/cells.parquet`, anchors and drill rollups. But the **row-group indexes over those files are FS instances**, hand-rolled four ways:
- `sidecar.parquet`: per RG `(file, rg, s_min, s_max, offset, length, rows)` over all `sx` shards ([`static_names.py`]:708-726). This is literally a parquet of footers for many files, but the TS hot path doesn't use it; the Python `Reader` (:759-812) and DuckDB ([`static_drill.py`]:532) do.
- The TS reader instead cuts a `GroupIndex {sMin, sMax, rows, chunks}` from each shard's own footer with a minimal Thrift-compact walker (`groupIndex`, [`staticNames.ts`]:148-195). It caches that index in an isolate LRU and the colo (:412-443), selects RGs (`selectGroups` :199), then does one ranged GET and decodes via a synthetic `FileMetaData`.
- `catalog/index.parquet`: per RG `rg, q_min, q_max, offset, length, rows, chunks` over `cells.parquet` ([`static_catalog.py`]; read whole by [`staticCatalog.ts`]:1-50).
- `anchors/…-index[.top].parquet` and `drill/…-index[.top].parquet`: **two-level** RG indexes (`static_roots.write_index_levels` :440-469). The `.top` level indexes the index.
- `shards.json` (3-char prefix → shard) is app partitioning, not an FS.

The spec that introduced it planned exactly this ("same footers-in-D1 pattern", [`static-name-search.md`]:77-86), then chose per-file footers plus colo caching.

### 1.5 disky: caches, and how they relate to an FS

An FS answers "which bytes". Caches answer "have I already fetched or decoded these bytes". They are **orthogonal layers in front of any FS back end and any data file**:

| Layer | disky today | Key | Generic? |
|---|---|---|---|
| Isolate LRU of decoded RGs | `groupCache` 24 MiB, `footerCache` 12 MiB ([`index.ts`]:1529-1576) | `(store, scan, variant, gen, rg)`; iv: no scan | yes |
| Isolate memo of handles and footers | `handles` (TTL 60 s, :394-398), `ivFooters` (:486) | pointer / file key | yes |
| Request-owned in-flight memo | `shared`, `inFlight`, `join`, `track` ([`shared.ts`]:1-245): a cancelled request's pending promises are evicted, and joins are bounded and budgeted | same | yes, and the hardest-won piece |
| Colo Cache API: byte ranges | `cachedRange` ([`index.ts`]:906) | `index-footer.cache/<key>?r=s-e` | yes (immutable objects) |
| Colo: footer bytes, compact footer, footer docs, decoded RGs | `readFooterBytes` (:918), `openIvFooter`, `ivFooterDoc`, `coloGroup` | `?footer`, `?fd=n&d=2`, `?rg=n` | yes |
| Colo + KV: response cache | [`edgeCache.ts`] | the request's semantic key | no: app-level, above the FS |

The FS client should **own** the first five layers as opt-in hooks (§4.5). The response cache stays in the app.

### 1.6 awair: a raw-tail variant

- `pyramid_shards` (pyrmts-cfw's shard registry, DDL copied from `D1ShardIndex.schemaSql()`) gains `size_bytes, n_rows, n_rgs, rg_row_counts` ([`awair 0003`]) and `footer_bytes BLOB`, the last 64 KiB of each shard ([`awair 0004`]:14).
- **Writers:** the cascade Worker slices the tail of the buffer it just encoded ([`write.ts`]:243-253, :342-350; [`cascade.ts`]:165-186). A TS backfill range-GETs missing tails ([`backfill.ts`]:35-78). A Python backfill covers Lambda-written raw shards (`stats_backfill`, [`pyramid.py`]:369-456).
- **Reader:** `wrappedStorage` ([`awair serve`]:144-230) wraps pyrmts' `Storage.getRange`. A request for the shard's last ≤64 KiB is served from D1, which matches pyrmts' `DEFAULT_INITIAL_FETCH_SIZE = 64 KiB` ([`fetch.ts`]:72). It is **transparent**: hyparquet still parses the footer.
- **Invalidation:** none beyond `size_bytes` equality. A growing raw shard misses.
- **Savings:** "~48% of query bytes and latency" ([`awair 0004`]:5-7).
- **Why this variant is legitimate:** awair's shards are single-RG with tiny footers. Parsing is cheap; the round trip is the cost. A parsed FS would add schema and stats machinery to save a parse of a few KB.

### 1.7 ctbk: the closest generic D1 FS

- **Schema** ([`rg-manifest.md`]:41-57; no checked-in migration):
  - `rg_manifest`: PK `(pyramid, key, rg_idx)`; `shard_written_at, row_start, num_rows, byte_start, byte_end`; stats `cell_min/max` (TEXT) and `dt_min/max`; `chunk_meta` (verbose per-column JSON, ~1 KB/RG); index `(pyramid, key, cell_min, cell_max)`.
  - `rg_manifest_schema(pyramid, schema_json)`.
  - `rg_manifest_fills(…, n_rgs, filled_at)`: a completeness sentinel, written last.
- **Writer:** TS in the API Worker, filled lazily from the footer (`fillManifestInner`, [`rg_manifest.ts`]:404-477, via `ctx.waitUntil` :294-345). Python only drives the backfill ([`gbfs_cli.py`]:205-275).
- **Reader:** `fetchShardRows` (:192-243) runs one `db.batch` with the fill check, chunked match queries (45 tokens per chunk, for the 100-bind cap) and the schema lookup. `decodeManifestRgs` (:245-270) builds a synthetic `FileMetaData`. Any failure deletes the key's rows and falls back to the footer, so the manifest is a cache, not the authority.
- **Why:** 245-285 MB shards, 5.6-6.3K RGs, 7-8 MB footers; hyparquet's object graph is ~10× that and OOMed 128 MB isolates. Cold latency went from ~4.5 s to ~0.5 s. 699,715 manifest rows cost ~$1.40 of D1 writes.

### 1.8 pyrmts, hyparquet fork, pqtk, others

- **pyrmts** ([`fetch.ts`]):
  - `MetadataCache` / `CachedMetadata {etag, size, metadata}` (:39-51) is a pluggable decoded-footer cache. Data GETs carry `If-Match`, and an `EtagConflict` invalidates the entry (:83-127).
  - [`walkdiff.ts`] has `RowGroupSummary` (:92), described as "a consumer stores these in D1 / a manifest blob so the edge never parses the footer", and `RowGroupIndex` (:105). `readRun` (:331-342) builds a synthetic `FileMetaData`.
  - Stats are hard-coded to `(depth, path)`. The D1 shard registry ([`shard-index.ts`]:216-250) has **no** footer columns.
  - Consumers: disky `#ad35ff8` (health page only), awair, ctbk.
- **hyparquet fork** (`~/c/hyparquet`; upstream `h/master`, fork `r/main`, local 1.31.1). Fork-only:
  - `metadataColumns` (`400f6fd`; src/metadata.js:15-20, :143-145; a skipped chunk throws on read, src/plan.js:148);
  - `suffixFetchSize` (`9ce569d`; src/utils.js:200-216);
  - `suffixStart` (`0396cce`);
  - cross-RG range coalescing via `maxOverfetchRatio` / `maxRunBytes` (`d999e10`; src/plan.js:448).
  - **Read API:** the read options take `metadata?: FileMetaData` (src/types.d.ts:42; src/read.js:33 parses only if it's absent). That is the seam every synthetic-footer reader above uses.
  - **Not exported:** `readRowGroup` (src/rowgroup.js:20) and `parquetPlan` (src/plan.js:21).
  - **Not there:** a subset-`FileMetaData` builder, and lazy `row_groups` (open in [`lazy-footer-parse.md`] §84, which itself points at pyrmts' `RowGroupIndex` as the caller-side equivalent).
  - **Dist builds:** `57f1078` has `metadataColumns`; nothing yet for `suffixFetchSize`.
  - **disky's site is on npm `hyparquet ^1.27.1` → 1.30.0, not the fork** ([`site package.json`]:30).
- **pqtk** (`~/c/pqtk`, 445 LOC, click + pyarrow + fsspec): one real command, `re-rg` (pqtk/cli.py:44), with `local` and `gcp-batch` runners (pqtk/runners.py). There is no footer or manifest command. `specs/content-hash-and-diff.md` proposes footer-only `content-hash` and `diff`.
- **Others:**
  - hccs/crashes `cells-api/src/parquet.ts:12-36`: an isolate LRU of parsed footers, 24 entries.
  - `~/c/js/file-tree` `src/renderers/parquetData.ts:39-68, :188-226`: in-browser per-RG stats plus a decoded-RG LRU over one footer read.
  - hccs/path: whole-file GETs, no footers.

## 2. Data model

### 2.1 What every store above actually records

| Concept | disky D1 | disky parquet | ctbk | DuckLake | Iceberg |
|---|---|---|---|---|---|
| Dataset / version | `(date, variant, gen)` + pointer | generation dir, `manifests/<D>.json` | `pyramid` | `ducklake_snapshot` | snapshot → manifest list |
| File | implicit (one per pointer) | implicit (file beside it) | `(pyramid, key)` + `written_at` | `ducklake_data_file` (`path, record_count, file_size_bytes, footer_size, begin/end_snapshot`) | manifest entry (`file_path, file_size_in_bytes, record_count`) |
| Schema | `schema_json` per pointer | kv `schema` | `rg_manifest_schema` per pyramid | `ducklake_column` (ids, versioned by snapshot) | schema id + field ids |
| Per-file column stats | none | none | none | `ducklake_file_column_statistics` (`min_value, max_value, null_count, value_count, column_size_bytes`) | `lower_bounds, upper_bounds, null_value_counts, column_sizes` |
| Per-RG position | `row_start/end` | same | `row_start, num_rows` | none | `split_offsets` (RG start bytes only) |
| Per-RG stats | fixed d/p/b/u columns | + `seg, vf, vt, b_min` | `cell`, `dt` | **none** | **none** |
| Per-chunk offsets | `rg_json` triples | same | `chunk_meta` | none (DuckDB reads the footer) | none |

**The key fact:** both lakehouse catalogs stop at **file** granularity. DuckLake stores `footer_size` so DuckDB can fetch the footer in one read, but DuckDB still reads every footer it touches. Iceberg's `split_offsets` locates RGs but carries no per-RG stats. Every store of Ryan's exists to go one level further, to RG and chunk granularity, so that a 128 MB isolate never parses a multi-MB footer. **The FS is a RG-granular catalog.** Lakehouse compatibility (§3.4) can only cover its file-level half.

### 2.2 Recommended schema

The schema has four relations. D1 holds them as tables (§3.1). The parquet back end holds the same columns (§3.2).

**`fs_dataset`**: a named, immutable set of files, such as a disky generation, an interval-store run, one ctbk pyramid's shard set at a fill, or a static-names generation.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE | e.g. `disky/gcs/iv/2026-10-09b`, `disky/gcs/scan/2026-10-09/g7` |
| `parent` | INTEGER NULL | a run's base generation; tiering chains |
| `stats` | TEXT (JSON) | the **slot map** (§2.3): `[{"col":"depth","slot":0,"agg":"minmax"}, {"col":"path","slot":1}, {"col":"size","slot":2,"agg":"max"}, …]` |
| `sort` | TEXT (JSON) | declared sort columns, e.g. `["depth","path"]`; enables the lexicographic planner (§4.3) |
| `tier` | TEXT | `d1`, or `pq:<uri>`: where this dataset's RG rows live (§3.3) |
| `kv` | TEXT (JSON) | app metadata (disky `version`, `floor_bytes`, interval `segments`) |
| `created` | INTEGER | epoch ms |

**`fs_pointer`** (`name` TEXT PK, `dataset` INTEGER, `updated` INTEGER): the atomic flip that `index_schema.gen` is today. Writers land a dataset fully, then flip.

**`fs_schema`** (`id` = first 16 hex of SHA-256 over canonical `schema_json`, `schema_json`): hyparquet `SchemaElement[]`, deduplicated. Many files share one schema, so storing it once per file (as the cold tier's kv does) or once per pointer (as `index_schema` does) both disappear.

**`fs_file`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | small: keeps RG rows small |
| `dataset` | INTEGER | |
| `uri` | TEXT | relative to a dataset `root` in `kv`, or absolute (`gs://`, `r2://`, `s3://`) |
| `size` | INTEGER | object size; also the suffix-read anchor |
| `etag` | TEXT NULL | the object's etag, or GCS generation; checked by `If-Match` on reads (pyrmts' pattern), optional for immutable keys |
| `schema` | TEXT | → `fs_schema.id` |
| `num_rows`, `n_rg` | INTEGER | |
| `footer_len` | INTEGER | lets a fallback reader fetch the real footer in one exact read (DuckLake's `footer_size`) |
| `codec` | TEXT NULL | when uniform (the common case); else per RG |
| `kv` | TEXT (JSON) | the file's `key_value_metadata`, or an allowlisted subset of it |
| `ord` | INTEGER | file order within the dataset (shard number, sort, run level) |

**`fs_rg`**: one row per row group. It is the hot table.

| Column | Type | Notes |
|---|---|---|
| `file`, `rg` | INTEGER | PK `(file, rg)`, `WITHOUT ROWID` |
| `row_start`, `num_rows` | INTEGER | absolute row position |
| `byte_start`, `byte_end` | INTEGER | the span of all chunks (ctbk's; disky derives it per projection) |
| `s0_lo, s0_hi, …, s5_lo, s5_hi` | untyped (SQLite `ANY` affinity) | the **stat slots** (§2.3); NULL means unknown, so the RG is always a candidate |
| `x` | TEXT NULL (JSON) | stats beyond six slots, unindexed (disky's `seg`, `b_min`) |
| `chunks` | TEXT | compact chunk record (below) |

**`chunks`** is a generalization of disky's `rg_json`: `[codec, [[dpo, size, dict|0, (ci_off, ci_len, oi_off, oi_len)?], …]]`, one entry per leaf in schema order.
- `num_rows` moves to its own column. The page-index offsets are an optional 4-tuple tail, present only when the writer opted in.
- It is about 250 B for disky's 14 columns. ctbk's verbose `chunk_meta` (~1 KB) would shrink about 4×: hyparquet needs only `type`, `path_in_schema` (both from the schema), `codec`, the offsets and `total_compressed_size`. Note that ctbk fills `total_uncompressed_size` with the compressed size.
- A per-column-chunk *table* (Iceberg-style `column_sizes` rows) is rejected: it multiplies D1 rows by the column count for data that is read only as a whole RG.

**Versioning and immutability:**
- Files are immutable: a rewrite is a new `fs_file` in a new dataset.
- Datasets are append-only once their pointer flips.
- GC deletes datasets no pointer names, after a grace period longer than the reader's handle TTL (disky's 60 s rule, [`index.ts`]:389-393).
- `etag` covers the stores whose keys are not content-addressed (awair's growing raw shards, ctbk's rewritten shards). Content-addressed or generation-dir keys leave it NULL.

### 2.3 Stat slots, not per-column tables

D1 must answer "which RGs of file F (or dataset D) overlap this key range", using an index. That rules out an EAV `(file, rg, col, min, max)` table: multi-column predicates would need self-joins, and it would cost about 3× the rows.

Instead, each dataset declares which columns get stats, and in which **slot**. SQLite compares an untyped column by storage class (INTEGER < TEXT, TEXT bytewise under BINARY collation), which matches parquet's UTF-8 byte order for strings and numeric order for ints. So one slot column can hold disky's `depth` in one dataset and ctbk's `cell` in another.

| Dataset | s0 | s1 | s2 | s3 | s4 | s5 | `x` |
|---|---|---|---|---|---|---|---|
| disky path/bysize | `depth` | `path` | `size` (hi only = `b_max`) | `usr` | | | `b_min` |
| disky interval | `depth` | `path` | `size` | `usr` | `vf` | `vt` | `seg`, `b_min` |
| disky static sx | `s` | | | | | | |
| ctbk rides | `cell` | `dt` | | | | | |
| awair (if adopted) | `ts` | | | | | | |

Indexes are fixed: `(file, s0_lo, s0_hi)` and `(file, s1_lo, s1_hi)`. A dataset puts its most selective planner column in s0. Six slots and two indexes cover every store above; adding a slot later is an `ALTER TABLE ADD COLUMN` with a NULL default, which rewrites no rows.

**Exactness.** Stats extracted post hoc come from footer statistics, which writers may truncate or drop. parquet-cpp drops a stat above `max_statistics_size`, and long paths can hit that. Missing stats become NULL, meaning "always a candidate". A writer that builds the FS *while* writing (§5) should compute **exact bounds from the rows**, as the interval store does ([`interval_store.py`]:443). The planner never trusts a bound to be tight, only to be conservative.

## 3. Back ends

### 3.1 D1

- **Layout.** The four tables above, plus `fs_pointer`. Everything is keyed by small integers: today's `index_row_groups` repeats `(store, date, variant, gen)` as TEXT in every row *and* in both secondary indexes, which is much of its ~766-830 B/row.
- **Size estimate** for a disky path-sort RG:
  - row: `file` + `rg` ~6 B, positions ~16 B, slots (two ~60 B path strings, small ints) ~140 B, `chunks` ~240 B, so ~400 B;
  - the two slot indexes repeat `file` plus their slot pair, ~150 B;
  - total **~550-600 B/row**, against ~800 B measured today. That is a ~25-30% cut, so `INDEX_RETAIN` 30 → ~40 at the same bytes.
  This is an estimate; measure it on a copy (§8).
  It is not a fix for the cap: **D1's per-scan cost is intrinsic to "every RG of every scan"**, and the interval store's answer (one store for all scans) is the real lever.
- **Planner queries.**
  - `SELECT rg, row_start, num_rows, s0_lo, … FROM fs_rg WHERE file = ? AND (<slot predicate>) ORDER BY rg LIMIT cap+1`, then `SELECT rg, chunks … WHERE file = ? AND rg IN (…)` for the survivors only. That is disky's two-phase shape ([`index.ts`]:1820, :1846). The phases keep the stats scan narrow; `chunks` is the wide column.
  - Multi-file plans use `file IN (…)` in chunks of 90 (D1's bind limit is 100), batched in one `db.batch`.
- **Writes** (Python, HTTP API): reuse `index_footer.py`'s machinery (`_d1_query` with retry and backoff, `_pack` to ≤64 KB statements, literal values to dodge the bind cap), moved to the writer library (§5).
  - Order: `fs_schema` and `fs_file` upserts, then `fs_rg` inserts, then `fs_dataset.tier='d1'`, then the `fs_pointer` flip, last. That is index-sync's discipline, and ctbk's `fills` sentinel generalized.
  - ~95K RGs at ~100 rows per request is ~950 requests per sort; that is today's cost.
- **Limits to respect:** 10 GB per database; 100 KB per statement; 100 bound parameters; 2 MB per row/BLOB (irrelevant at ~0.5 KB); never rebuild a large table (`DROP COLUMN` hit the statement limit, [`0006_store_scoped_index.sql`]:19-21); D1 enforces foreign keys, so declare none.

### 3.2 Parquet of footers (R2, or any object store)

One FS dataset is two parquet files under a prefix:
- `files.parquet`: the `fs_file` rows, small (one row per data file), read whole and cached.
- `rg.parquet`: the `fs_rg` rows of **every** file in the dataset, sorted `(file, rg)`, so one file's RGs are contiguous. Within a sort, consecutive files are adjacent too.
  - Footer groups are 512 rows (today's `FOOTER_ROW_GROUP_ROWS`), zstd.
  - Statistics are on `file` and the slot columns only. `store_schema=False`, as today.
  - The kv metadata holds the `fs_dataset` row (`stats`, `sort`, `kv`) and the `fs_schema` rows. It is self-describing, as `.groups.parquet` already is ([`groups.py`]:258-275).

This is today's `.groups.parquet` with three changes:
- It covers **N files**, not one, so one per generation in place of one per sort and per run.
- The columns are generic slots.
- It has a `file` column.

Reading follows the existing `pq` mode exactly:
1. One suffix GET of `rg.parquet`'s own footer (`ByteStore.tail`, [`index.ts`]:894, 925-934).
2. Prune footer groups by their stats.
3. Range-read and decode the survivors, `chunks` lazily (`readFooterCols`, :1014).

The colo compact-footer and footer-doc layers (§1.3) carry over unchanged.

**When `rg.parquet`'s own footer gets big** (cw's 6,987 RGs → 14 footer groups is fine; a fleet-wide FS of ~10M RGs → ~20K footer groups → a multi-MB footer, the problem it exists to solve): cut a **second level**, `rg.top.parquet`, an FS of `rg.parquet` itself. The static index already does this (`*.top.parquet`, §1.4). The client walks levels until a footer fits `suffixFetchSize`. The recursion is free because the FS *is* parquet.

**Cost:** ~170-190 B per RG ([`path-store.md`]:102, :106), about 4× denser than D1, and R2 storage at $0.015/GB-month is noise. The price is one extra round trip per cold colo (the FS footer) and a footer-group decode.

### 3.3 Tiering and discovery

**Hot means D1 and cold means parquet; both hold identical rows.** The writer always writes the parquet (it is the durable record and the input to D1 sync), and optionally syncs it into D1. Retention deletes `fs_rg` rows for old datasets and sets `fs_dataset.tier = 'pq:<uri>'`. `fs_dataset` and `fs_file` rows stay in D1 forever, since they are small.

**Discovery:** the client opens a dataset by name, or by a pointer to it. With a D1 binding, it reads the `fs_dataset` row (cached per isolate, TTL), and `tier` says where the RG rows are. That replaces today's `SELECT 1 … LIMIT 1` probe ([`index.ts`]:416) with an explicit column. Without D1 (the r2 demo, the interval store today), the spec names the parquet URI directly. The order is always D1 when `tier='d1'`, else the parquet. The legacy `.groups.json` blob mode is not carried forward (§7).

### 3.4 DuckLake alignment

**What DuckLake is:** a catalog in a SQL database (DuckDB, SQLite, Postgres or MySQL) with data as parquet. The relevant tables are `ducklake_snapshot`, `ducklake_table`, `ducklake_column`, `ducklake_data_file` (`data_file_id, table_id, begin_snapshot, end_snapshot, path, path_is_relative, file_format, record_count, file_size_bytes, footer_size, row_id_start, …`), and `ducklake_file_column_statistics` (`data_file_id, table_id, column_id, column_size_bytes, value_count, null_count, min_value, max_value, contains_nan`). External files can be registered with `ducklake_add_data_files`, using name mapping when the files carry no field ids. These names are from the DuckLake 0.x spec as I know it (mid-2025); **verify against the current spec before building anything.**

**The mapping:**

| FS | DuckLake |
|---|---|
| `fs_dataset` + `fs_pointer` | `ducklake_table` + a `ducklake_snapshot` per flip |
| `fs_file` | `ducklake_data_file` (direct: `path`, `record_count`, `file_size_bytes`, `footer_size`) |
| `fs_schema` | `ducklake_column` rows (needs stable column ids) |
| per-file aggregate of `fs_rg` slots | `ducklake_file_column_statistics` (min of `lo`, max of `hi`, per stat column) |
| `fs_rg`, `chunks` | **no equivalent**; extension tables DuckDB ignores |

**Recommendation: export, don't align.**
- Make `pqtk footers ducklake -o catalog.sqlite <fs uri>` write a DuckLake catalog (SQLite flavor) that points at the same parquet URIs, with per-file stats rolled up from `fs_rg`. DuckDB then `ATTACH 'ducklake:sqlite:catalog.sqlite'` and queries all of a dataset's files as one table, with file-level pruning.
- That is useful for ad-hoc analysis on a node: it replaces `read_parquet([… 400 globs …])` and gets file pruning for free.

**Why not make the live D1 schema DuckLake's:**
1. DuckDB can't attach D1. D1 is SQLite behind an HTTP API with no file or wire access. An attachable copy is an export (`wrangler d1 export`) anyway.
2. DuckLake's schema carries snapshot/column-id/change-tracking machinery (`begin_snapshot/end_snapshot` on columns and files, `ducklake_snapshot_changes`, delete files, inlined data). That machinery is dead weight for immutable generation sets, and it would add D1 bytes to the hot table.
3. DuckLake has no RG level, which is the part that matters at the edge, and DuckDB would read the real footers regardless.
4. It would couple the FS's evolution to DuckLake's spec churn (0.x).

**Cost of the export:** ~150 lines of Python (snapshot 1, one table per dataset, columns from `fs_schema`, files, rolled-up stats) plus a round-trip test (DuckDB attaches, counts rows, prunes by a stat). It is deferred until someone wants it (§7, step 6).

## 4. TypeScript read client

### 4.1 Shape

```ts
type FsSpec =
  | { d1: D1Database; dataset: string | { pointer: string }; bytes: ByteStoreFor; fallback?: ByteStore }
  | { pq: { store: ByteStore; prefix: string }; bytes: ByteStoreFor }

/** Where data files are read from: by URI scheme (r2 binding, S3Store over GCS/R2/CAIOS, fetch). */
type ByteStoreFor = (uri: string) => ByteStore  // disky's `ByteStore` (index.ts:151): get(range), tail?(n)

function openFooterStore(spec: FsSpec, opts?: { cache?: FsCache; trace?: Trace; req?: Req }): Promise<FooterStore>

interface FooterStore {
  dataset: FsDataset                        // stats slot map, sort, kv, tier
  files(filter?: { ord?: [number, number]; uri?: (u: string) => boolean; stats?: Pred }): Promise<FsFile[]>
  plan(q: PlanQuery): Promise<ReadPlan>
  read<R = Record<string, unknown>>(plan: ReadPlan, opts?: ReadOpts<R>): AsyncIterable<GroupRows<R>>
  schema(file: FsFile): SchemaElement[]
}

interface PlanQuery {
  files?: FsFile[] | number[]               // default: every file of the dataset
  columns?: string[]                        // projection (chunk spans and hyparquet `columns`)
  where?: Pred                              // over stat columns, by NAME (the client maps names → slots)
  keyRange?: { lo: unknown[]; hi: unknown[] }  // over the declared sort columns (§4.3)
  rgFilter?: (g: RgStats) => boolean        // app-level post-filter on decoded bounds (e.g. disky's sizeNeighbours)
  cap?: number                              // TooWide past it
}

interface ReadPlan {
  groups: { file: FsFile; rg: number; rowStart: number; numRows: number; start: number; end: number; bounds: RgStats }[]
  rows: number                              // what a read would decode (planSubtree's comparator)
  bytes: number
}

interface ReadOpts<R> {
  runs?: { gap: number; max: number; inFlight: number }   // default disky's RUN_GAP/RUN_BYTES/RUN_READS
  rowFilter?: (r: R) => boolean
  stop?: () => boolean                      // abandon unfetched groups (readGroupsCached's `stop`)
  decoded?: 'rows' | 'columns'              // GroupDoc-style columnar, or objects
}
```

`Pred` is a small AST, `{ and | or: Pred[] } | { col, op: 'overlaps', lo, hi } | { col, op: '>=' | '<=' | '=', v } | { col, op: 'max>=', v }`. It compiles **twice**:
- to SQL over the slot columns (D1);
- to a JS function over decoded bounds (the parquet back end's footer-group pruning, and per-RG pruning).

Both must be monotone in the bounds, which is disky's soundness argument for footer-group pruning ([`index.ts`]:1150-1156), and a shared test asserts that the SQL and JS forms agree on random bounds.

### 4.2 Read path

`read` is today's `readGroupsCached` ([`index.ts`]:1614-1714) with the disky types removed:
1. Look up each group in the decoded-RG cache (isolate, then colo).
2. For misses, take `chunkSpan` over the projection and `planRuns` to merge spans across RGs and files of the same URI.
3. Fetch at most `inFlight` runs at a time.
4. Decode each group with a **synthetic `FileMetaData`** (`{version, schema, num_rows, row_groups:[revived], metadata_length:0}`) via `parquetReadObjects({file: bufferSlice(...), metadata, columns, compressors})`.

`metadataColumns` is moot on this path: a synthetic footer holds only the projected chunks. It matters for the **fallback**: a file that the FS lacks (a miss, ctbk-style) is opened with its real footer and `metadataColumns: columns`, which keeps a 7 MB footer's object graph at the projected columns.

### 4.3 Planner primitives the client owns

- **Lexicographic key ranges over the sort columns** (`keyRange`). Per-column min/max can't express "a tuple range over `(depth, path)`" directly. The conservative test, which disky hand-wrote as `d_max >= lo AND d_min <= hi AND (d_min <> d_max OR (p_max >= pLo AND p_min <= pHi))` ([`index.ts`]:1797), generalizes: a group overlaps `[lo, hi]` if the leading columns' ranges overlap and, wherever a leading column is constant in the group, the next column's range overlaps too. The client generates this for any declared sort, in SQL and JS. disky's rects, ctbk's `(cell, dt)` and the static index's `s` ranges become `keyRange` calls.
- **Two-phase D1 plans** (stats, then `chunks` for the survivors) and **chunked `IN` lists** under the bind cap.
- **`planRuns`**, exported, so an app can merge spans **across plans** (e.g. both sides of a diff, or `path` + `bysize`) before reading: `read(mergePlans(a, b))`.
- **Two-level footer walking** for the parquet back end (§3.2).

### 4.4 App-level batching hooks

The client doesn't guess at app concurrency. Instead it exposes:
- `plan` without `read`, so an app can compare plans (disky's `path` vs `bysize`) and hold reads to a group budget;
- `mergePlans`;
- `read` taking `runs` knobs;
- `stop`;
- `prefetch(plan)`: footers and footer groups only, the way `openInterval` warms sibling sorts ([`index.ts`]:691-713).

### 4.5 Caching hooks

`FsCache` is an interface with disky's implementations as the default for Workers:

```ts
interface FsCache {
  isolate: { rows: Lru; footers: Lru }      // byte-capped LRUs (index.ts:1545)
  colo?: Cache                              // caches.default: ranges, FS footers, footer docs, decoded groups
  inFlight: SharedMemo                      // shared.ts: request-owned, cancel-evicts, bounded joins
  key: (part: string) => string             // namespacing: store, generation, revision (ivRev)
}
```

Keys derive from `(fs_file.uri, etag|dataset, rg)`. Immutability makes every layer safe without invalidation, and the dataset name, which is a generation, is the version.

Request ownership comes from `shared.ts`'s `Req` on the env ([`shared.ts`]:48-80). The client accepts a `req` and threads it through, so a cancelled viewer's work is evicted the way it is today. This module is the most valuable thing to lift out of disky. It took two prod incidents to get right ([`shared.ts`]:1-21).

### 4.6 Instrumentation

`trace?: (name, ms, desc?) => void` ([`index.ts`]:99) is called with fixed phase names: `fs-open`, `fs-plan` (with `d1` or `pq` in `desc`), `fs-footer`, `fs-fgroup`, `fetch`, `decode`, `gcache`, `gcolo`, `gshared`, `join-<kind>`. The app's `serverTiming()` sink ([`edgeCache.ts`]) is unchanged.

### 4.7 What stays app-specific

| disky | Why it isn't FS |
|---|---|
| `sizeNeighbours` straddling-group pruning over `bysize` ([`index.ts`]:2100) | uses a group's neighbours' bounds and `seg`; passed in as `rgFilter` with a `prev/next` context |
| interval liveness `vf ≤ asOf < vt`, `combineLive` across runs (:1519) | row-level semantics of SCD-2 versions |
| depth bands, `thrAt(depth)` thresholds, `b_max` floors | query semantics; the app builds `Pred`s |
| `planSubtree`'s choice between sorts ([`view.ts`]:402) | uses `plan().rows` |
| `toRow` shaping, `IvGroup.pick` | row shapes |
| run/base tiering of the interval store | modeled as datasets with `parent`; *which* runs a scan reads is app logic |
| response cache ([`edgeCache.ts`]) | semantic keys above the FS |

## 5. Python write side

**Library** (`pqtk.footers`):

```python
def extract(uri: str, *, stats: list[StatSpec], fs=None) -> tuple[FileRow, list[RgRow], SchemaRow]
    # one footer read (pyarrow FileMetaData via fsspec, suffix read); stats from footer statistics
def from_writer(meta: pq.FileMetaData, uri: str, size: int, *, stats, exact: dict[int, dict] | None = None) -> ...
    # build during writing: the footer is in hand, no re-read; `exact` = per-RG bounds the writer computed from rows
def build(uris: Iterable[str], out: str, *, dataset: str, stats: list[StatSpec], sort: list[str] | None,
          parallel: int = 32, rg_rows: int = 512, top: bool | None = None) -> BuildReport
    # files.parquet + rg.parquet (+ rg.top.parquet when the footer would exceed ~256 KiB)
def sync_d1(fs_uri: str, *, db: D1Target, pointer: str | None, insert_bytes: int = 64_000) -> SyncReport
def retire_d1(db: D1Target, *, keep: Callable[[Dataset], bool]) -> RetireReport   # rows → tier='pq:…'
def gc_d1(db: D1Target, *, grace_s: int = 300) -> GcReport
```

`StatSpec` is `"depth"`, `"path"`, `"size:max"`, `"usr"`, `"vf:min"`, and so on. The slot order is the list order.

**CLI** (pqtk):

```bash
pqtk footers build -o r2://bucket/fs/<name> -d <dataset> -s depth,path,size:max,usr -k depth,path  URI|GLOB...
pqtk footers build -R gcp-batch …          # fan out the extraction for 10K+ files (pqtk's runner)
pqtk footers sync-d1 -D <db-id> -p <pointer> r2://bucket/fs/<name>
pqtk footers retire -D <db-id> -k 30 --prefix disky/gcs/scan/
pqtk footers gc -D <db-id>
pqtk footers show [-j] r2://bucket/fs/<name>        # datasets, files, RG counts, stats coverage, bytes
pqtk footers plan r2://bucket/fs/<name> -w 'path>=a/b/ & path<a/b0' -c path,size  # offline planner (disk-tree tiers plan, generalized)
pqtk footers verify r2://bucket/fs/<name>           # re-read N random files' real footers; assert offsets/stats agree
pqtk footers ducklake -o catalog.sqlite r2://bucket/fs/<name>   # §3.4, later
```

**Writers adopting it in place:**
- disky's `disk-tree tiers -g`, `dt-cloud path-index`, `interval-store cut|append` and `static-names` call `from_writer(..., exact=…)` as they write.
- Batch jobs that only have URIs call `build`.
- `index_footer.py`'s HTTP D1 code moves to `pqtk.footers.d1`. `dt-cloud index-sync` becomes a thin wrapper until it retires.

## 6. Where each piece lives

| Piece | Home | Why |
|---|---|---|
| Format spec (§2), fixtures | **pqtk** (`specs/footers-format.md`, `tests/fixtures/`) | the writer and reader of one format version together |
| Python extract/build/sync/retire + CLI | **pqtk** (`pqtk/footers/`) | Ryan's suggestion; pqtk already has fsspec and the Batch runner |
| TS client (`openFooterStore`, planner, D1 + parquet back ends, `FsCache`, `shared.ts` memo) | **pqtk, as a JS package** (`pqtk/js/footers`, npm name e.g. `pqtk-footers`, dist-branch pinned like `@rdub/treemap`) | it is a lower layer than pyrmts (pyramids depend on it, not vice versa); keeping it beside the Python writer gives one repo and version for the contract, and cross-language fixture tests (Python writes, vitest reads) in one CI |
| Upstream-able primitives | **hyparquet fork** (PRs to upstream) | `syntheticMetadata(schema, rowGroups, kv?)`; exported `readRowGroup` / `parquetPlan`; `chunkSpan(rg, columns)`; page-index-aware spans; lazy `row_groups` ([`lazy-footer-parse.md`] option 3) |
| Pyramid-specific use | **pyrmts** | `RowGroupIndex` / `MetadataCache` become thin adapters over an FS; `D1ShardIndex` can point at FS file ids (awair, ctbk) |

**Rejected alternatives:**
- **The client inside the hyparquet fork.** D1 bindings, Workers' Cache API and request-ownership semantics don't belong in a general parquet reader. Upstream would never take them, and the fork would drift further from `h/master`.
- **The client inside pyrmts-cfw.** It already has D1 helpers and all three consumers, but it would make a generic layer depend on (and version with) the pyramid library. disky's main use is not pyramids.

**Prerequisite:** disky's site must move from npm `hyparquet` 1.30.0 to the fork's dist (or upstream, once the primitives land), since the client depends on them.

## 7. Harmonization

Ryan's question was whether every store should move onto one blessed FS. **Mostly yes, but not all, and not at once.** The cost is migrations of live serving paths, each of which needs a parity gate. The payoff is the deletions.

| # | Store | Action | Parity gate | Deletes after |
|---|---|---|---|---|
| 0 | none | Build pqtk `footers` (Python + TS) and the hyparquet primitives. Fixture: re-express an existing gcs `.groups.parquet` and a cw one as FS `rg.parquet`. | `pqtk footers verify` on 200 random files of each store; the TS planner returns identical RG sets to `selectSpans1` / `pqGroups` on recorded queries | none |
| 1 | disky interval store | Write one FS per generation (all sorts and runs as files, runs as child datasets) in place of `<sort>.groups.parquet` per file; reader `pq` mode → FS client | `ab.test.ts` A/B: identical JSON on the §6.2/§6.4 sample (views, diffs, lenses, runs) ([`interval-store.md`]:425-431); `Server-Timing` p50/p95 no worse, warm and cold colo, on dev | `GROUPS_SCHEMA` writer, `openIvFooter` / `ivFooterDoc` / `CompactFooter` (absorbed into `FsCache`), `pqGroups` |
| 2 | disky static name index | Replace the four hand-rolled RG indexes (`sidecar.parquet`, the Thrift `groupIndex` cutter, `catalog/index.parquet`, `*.top.parquet`) with one FS per static generation | the existing static test suites + A/B on `/api/search` / filter / drill answers | `groupIndex` Thrift walker, `sidecar_rows`, `write_index_levels`, catalog index writer |
| 3 | ctbk `rg_manifest` | Swap its D1 tables for FS D1 tables, filled by `pqtk footers build` + `sync-d1` (or kept lazy: a TS `fill` that writes FS rows); keep the footer fallback | identical rows for its query set; cold p50 ≤ 0.6 s; D1 bytes per RG ~4× smaller | `rg_manifest.ts` fill/decode code, three tables |
| 4 | disky per-scan path store, D1 hot tier | **Don't migrate: retire** with the interval store ([`interval-store.md`] §4). If per-scan serving outlives that, its cold `.groups.parquet` become FS datasets (a sf re-cut) | same A/B as #1 | `index_schema` / `index_row_groups`, `index_footer.py`, `openBlob` and `.groups.json` (deletable once every pointed generation is shown to have a `.groups.parquet`; unverified here) |
| 5 | awair `footer_bytes` | **Leave as is.** Single-RG shards with small footers: the raw tail is the right shape, and it's transparent to pyrmts. Revisit only if awair grows multi-RG shards | none | none |
| 6 | DuckLake export | Optional, on demand | DuckDB attach + count + pruning test | none |

**Order rationale:**
- #1 first: it is disky's live direction, already parquet-of-footers with exact bounds, and the closest shape to the FS. It exercises multi-file datasets and parent chains.
- #2 next, because it consolidates four bespoke formats.
- #3 is a separate repo and session. Write a spec into `~/c/hccs/ctbk/specs/` once 0-1 land.
- #4 is a deletion, not a port. Porting a store that §4 of the interval spec proposes to retire would be wasted parity work.

**Candidly, where stores legitimately differ:**
- awair: the cache shape is optimal for its files.
- Per-scan D1: the hot tier exists only because per-scan stores repeat everything per scan. A D1 FS doesn't change that arithmetic, only the constant (~25-30%).
- The static name index's *data* files: the key schemes are app logic. Only the RG indexes over them converge.

## 8. Risks and open questions

- **D1 limits.** 10 GB per DB is the binding constraint, and the FS moves only the constant. Tiering (§3.3) is mandatory, not optional. Multi-GB tables can't be rebuilt, so the slot layout must be right before any big fill. Add slots by `ADD COLUMN` only. Measure B/row on a scratch D1 with one gcs generation's rows before committing to the estimate in §3.1.
- **Stats on many columns.** Each slot costs ~2× its value bytes per RG, plus an index's worth if indexed. Path strings dominate. Slots beyond s1 should be numeric or short. Wide string stats (e.g. `path` on `bysize`) are better left in the parquet tier.
- **Truncated or missing footer stats.** These are conservative (NULL = candidate), so they cost selectivity, never correctness. Writers that matter should pass exact bounds.
- **Schema evolution.** `fs_schema` is per file, so a dataset may mix schemas. The client must project per file, and a column missing from a file reads as null. `chunks` is positional against *that file's* schema, as `rg_json` is today ([`index.ts`]:1275).
- **Large integers.** Offsets are < 2^53, which is fine. Stat values that are int64 past 2^53 (e.g. ns timestamps) lose precision as JS numbers from D1. Store such slots as zero-padded TEXT or in ms.
- **hyparquet upstream vs fork.** The client needs only `metadata` passthrough (upstream has it) and `parquetReadObjects`. `syntheticMetadata` and exported planning helpers are nice-to-have PRs. Keep the client working on upstream hyparquet, with the fork as an optimization, so disky isn't pinned to a fork for correctness.
- **The FS footer is itself a cold-start cost.** A 0.5 MB `rg.parquet` footer on a cold colo is the interval store's known cold cost. The `.top` level and the compact-footer colo doc address it. A D1 `fs_dataset.kv` copy of the compact footer (~40 KB) would remove the R2 round trip, but measure first.
- **Ownership across sessions.** pqtk, pyrmts, hyparquet and ctbk are separate projects with their own sessions. This spec is disky's design input. Implementation in each repo goes through a spec in that repo (Ryan's spec workflow).
- **Open:** is `fs_file.etag` worth carrying for disky (generation dirs are immutable), or only for ctbk and awair? Should `fs_pointer` replace disky's `index_schema` pointers before #4, or only with the interval store's manifests? Does the r2 demo (no D1 FS today) get a D1 tier at all?

[`0001_init.sql`]: ../site/migrations/cw/0001_init.sql
[`0006_store_scoped_index.sql`]: ../site/migrations/cw/0006_store_scoped_index.sql
[`index_footer.py`]: ../cloud/src/dt_cloud/index_footer.py
[`cli.py`]: ../cloud/src/dt_cloud/cli.py
[`interval_store.py`]: ../cloud/src/dt_cloud/interval_store.py
[`interval_append.py`]: ../cloud/src/dt_cloud/interval_append.py
[`static_names.py`]: ../cloud/src/dt_cloud/static_names.py
[`static_catalog.py`]: ../cloud/src/dt_cloud/static_catalog.py
[`static_drill.py`]: ../cloud/src/dt_cloud/static_drill.py
[`groups.py`]: ../src/disk_tree/find/groups.py
[`tiers.py`]: ../src/disk_tree/cli/tiers.py
[`index.ts`]: ../site/functions/_lib/index.ts
[`view.ts`]: ../site/functions/_lib/view.ts
[`shared.ts`]: ../site/functions/_lib/shared.ts
[`edgeCache.ts`]: ../site/functions/_lib/edgeCache.ts
[`staticNames.ts`]: ../site/functions/_lib/staticNames.ts
[`staticCatalog.ts`]: ../site/functions/_lib/staticCatalog.ts
[`site package.json`]: ../site/package.json
[`path-store.md`]: path-store.md
[`interval-store.md`]: interval-store.md
[`index-landscape.md`]: index-landscape.md
[`static-name-search.md`]: architecture/static-name-search.md
[`awair 0003`]: ../../awair/cfw/cascade/migrations/0003_shard_stats.sql
[`awair 0004`]: ../../awair/cfw/cascade/migrations/0004_footer_cache.sql
[`write.ts`]: ../../awair/cfw/cascade/src/write.ts
[`cascade.ts`]: ../../awair/cfw/cascade/src/cascade.ts
[`backfill.ts`]: ../../awair/cfw/cascade/src/backfill.ts
[`pyramid.py`]: ../../awair/src/awair/cli/pyramid.py
[`awair serve`]: ../../awair/cfw/serve/src/index.ts
[`rg-manifest.md`]: ../../hccs/ctbk/specs/rg-manifest.md
[`rg_manifest.ts`]: ../../hccs/ctbk/gbfs/api/src/rg_manifest.ts
[`gbfs_cli.py`]: ../../hccs/ctbk/ctbk/gbfs_cli.py
[`fetch.ts`]: ../../pyrmts/js/packages/pyrmts/src/fetch.ts
[`walkdiff.ts`]: ../../pyrmts/js/packages/pyrmts/src/walkdiff.ts
[`shard-index.ts`]: ../../pyrmts/js/packages/pyrmts-cfw/src/shard-index.ts
[`lazy-footer-parse.md`]: ../../hyparquet/specs/lazy-footer-parse.md
