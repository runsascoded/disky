# Slimmer layer-2 listings: drop redundant columns, zstd, then cross-scan consolidation

Status: **phase 1 implemented** on the base (2026-09-29, in-memory writers the same day); phases 2 and 3 open. Proposed 2026-09-29 from the cw-s3 session (Ryan: "yes spec it, this is a good direction").

## Motivation

The per-scan, per-bucket layer-2 listing (one row per object and dir) dominates our storage: on cw it is ≈ 3.6 of each scan's 4.2 GiB, about 250 GiB across 100 scans, mirrored to R2 as well. gcs's `listing/` (2.45 TiB) is very likely the same format. The indexes the site serves are the small part (≈ 0.56 GiB per cw scan).

Measured on `cw-l2/2026-09-29T1201/marin-us-east-02a.parquet` (3.58 GiB, 54.2 M rows, 207 row groups, **Snappy**):

| column | compressed | uncompressed | note |
|---|---|---|---|
| `uri` | 1447 MiB | 9529 MiB | **exactly** `s3://<bucket>/` + `path` (verified on 524k rows) |
| `path` | 1372 MiB | 8340 MiB | |
| `parent` | 252 MiB | 3100 MiB | derivable from `path` (`_PARENT_EXPR`) |
| `size` | 153 MiB | 224 MiB | |
| `sum_storage_class_id_1` | 153 MiB | 224 MiB | **equals `size`** (single storage class) |
| `mtime_mean`, `mtime` | 151 / 127 MiB | | |
| `n_desc`, `n_files`, `n_children`, `kind`, `depth` | < 3 MiB each | | |

Rewrite experiment (2 row groups, 524k rows, pyarrow):

| variant | size vs plain rewrite |
|---|---|
| snappy, all columns | 100 % |
| zstd-3, all columns | 36.7 % |
| snappy, − `uri` − `sum_storage_class_id_1` | 61.1 % |
| **zstd-3, − both** | **23.4 %** |
| zstd-9, − both | 21.3 % |

The plain rewrite is itself smaller than the original (≈ 24 vs ≈ 37 MiB for those two row groups), so against today's files the slim zstd variant is ≈ 15 %. Estimate: cw's ≈ 250 GiB of listings → ≈ 40–55 GiB, **lossless**.

## Phase 1: writer (base engine, `src/disk_tree/find/aggregate_duckdb.py`)

- Stop writing `uri` (lines ~188, 357–378). Readers that need it compute `scan_root || '/' || path`. First **audit every reader** of `uri`, `parent` and the `sum_storage_class_id_*` columns: engine (`server.py`, `blobfs.py`, `sqla/model.py`, `cli/{snapshots,index,capture,import_listing}.py`, `find/{index,agg_ext}.py`), the `cloud/` overlay (`cascade_a2a.py` uses the class columns), the site's `/files` parquet viewer, and the plan-sweep / sweep manifest builders. Where a reader needs `uri`, derive it at read time. Keep a schema version in the parquet KV metadata so readers can tell old from new.
- Keep `parent` for now: it is cheap after zstd, and many engine paths group on it. Revisit after measuring zstd-with vs zstd-without.
- `sum_storage_class_id_<k>`: write only classes that are present and non-trivial. A single-class store (cw) writes none, and readers treat "absent" as "all bytes in the default class".
- **zstd** (level 3 default; measure 6/9 for the write-time CPU trade) for every layer-2 and index parquet the engine and overlay write. The overlay already uses zstd for access and reactive outputs.
- Verify with byte-for-byte identical *indexes* (`path-index`, coarse tiers, age pyramid) built from an old-format vs a new-format listing of the same scan.

### Phase 1: as implemented

**Format.** `src/disk_tree/listing_format.py`. A v2 listing carries parquet key-value metadata; a v1 listing has none of these keys (that absence *is* the v1 marker):

| key | value |
|---|---|
| `disk_tree.listing_format` | `2` |
| `disk_tree.scan_root` | the scan root (`s3://bucket`, `gcs://b`, a bare local path); `uri` = root at `.`, else `root/path` |
| `disk_tree.implied` | JSON `{column: source}` for columns left out because they equal `source` on every row, e.g. `{"sum_storage_class_id_1":"size"}` (omitted when empty) |
| `disk_tree.columns` | JSON list: the v1 column order, so a restored frame matches a v1 writer's column for column |

Columns: v1 minus `uri` minus the implied pivots.

**Codec switch: zstd by default since 2026-09-30** (opt-in until `@rdub/file-tree` decoded it). One switch, `$DISK_TREE_PARQUET_CODEC` (`zstd` by default at level 3, `ZSTD_LEVEL`; `snappy` opts out), is read in one place (`listing_format.codec()` / `duckdb_codec()` / `pyarrow_codec()`). It governs every writer phase 1 touched: the layer-2 listing (both engines), the engine tiers, and the overlay's served indexes (`dt_cloud.index.duckdb_codec`, a lazy wrapper so the CLI still imports without the engine). The column/metadata changes above do not depend on it. The default flipped once `@rdub/file-tree` decoded zstd (its spec `zstd-parquet.md`, done 2026-09-30: `defaultCompressors` via `fzstd`; `site/` pins `dist.cef98b1`). **Deploy order per deployment: the site (the pin) before the job image that writes zstd**, or `/files` shows a decode error on every new file until it does. `migrate-row-groups` keeps each file's own codec; `recompress` re-encodes a v2 file under another codec (same columns and keys), so the flip reaches the already-slim files by re-running it. Levels 6/9 are not measured yet.

**"Present and non-trivial" pivots.** A `--pivot-sum` column is implied when the pivoted column holds exactly one value **and no NULLs** for the bucket: then every file row's pivot is its own size, folder placeholders likewise, synthesized dirs 0 = 0, and the cascade sums both alike, so `sum_<col>_<v> == size` on every row by construction (not checked after the fact). Such a pivot is not computed at all. With ≥ 2 values, or one value plus NULLs, every pivot column is written as before. "Absent" in a reader means "equals `size`" only when `implied` says so. A pivot missing without that entry keeps its old meaning (no bytes in that class, or no `-p`). Dropping the majority class of a multi-class bucket (derivable as `size − Σothers`) was not done.

**Writers changed:** the duckdb engine (`aggregate_listing_to_parquet`, including the ranged-sort concat) and the stream engine (`aggregate_stream`'s finalize; its intermediate parts still carry `uri`). A self-check after the duckdb COPY asserts the written columns equal the v2 contract. `write_tiers` copies the source's format keys onto every tier. The codec switch governs these writers: the listing, the tiers, and the overlay's served index parquet in `index.py` (path-index, coarse tiers, age index, age pyramid), `viz.py` (gcs path-index and coarse tiers, including the by-user copies), and `overtime.py`. Intermediates (`labels-*`, `dir_stats`, `age_days`, over-time shards) and sweep manifests are unchanged.

**In-memory writers (second pass, same day).** Every writer that saves a pandas frame or Arrow table as a scan blob goes through one helper, `listing_format.write_listing` (→ `slim_table`, then `blobfs.write_table` with the switch codec): `ParquetBackend.save` and `HybridBackend.save` (so local `disk-tree index`, `import -e pandas`, `sync`/`pull`'s pandas path, and every hybrid chunk), the hybrid delete rewrites (inside a chunk, a whole chunk, directly in the root), the server's `/api/delete` rewrite for a non-updating backend, and `migrate-depth` / `migrate-blobs`. `slim_table` names the scan root from the frame itself — the `.` row's `uri` — rather than from `Scan.path`, so a **hybrid chunk's root is the subtree's absolute location** (`/Users/ryan/Library` for the chunk cut at `Library`: chunk rows are rebased to `.`, their `uri`s are not), and restoring gives back exactly the `uri` column the chunk was cut with. It is lossless only if `uri == uri_of(root, path)` on every row, and that is **checked, vectorized (Arrow compute), on every row**; a frame that fails it — or has no `uri`, or no `.` row — is written unchanged as v1 with a stderr note (a v1 blob is always a correct fallback). A `sum_*` column equal to `size` on every row is left implied (checked after the fact here, since a pandas frame has no "by construction" pivot bookkeeping; still lossless). The v1 column order is the frame's own (the pandas engine puts pivots before `parent`, the duckdb/stream engines after), recorded in `columns` as before. The file's `pandas` schema metadata is trimmed to the columns written.

**`uri` per root.** `uri_of(root, path)` = `root` at `.`, else `uri_prefix(root) + path` where the prefix is `root/` — or `root` itself when it already ends with `/`. That last case is the filesystem root: `disk-tree index /` used to give dir rows `//foo` (`aggregate` joined with `f'{root}/{p}'`) while file rows had `/foo` (gfind's `%p`); `aggregate` now uses `uri_of`, so a `/` scan's `uri`s are consistent and slimmable. Verified exact for local (`/Users/ryan`, `/`), `s3://bkt`, `s3://bkt/pre/fix`, `r2://`, `gs://`, `gcs://` and `ssh://user@host:port/abs` roots (`tests/test_listing_writers.py`). `restore` and `uri_sql` use the same prefix rule; every existing v2 file has a bucket root, which never ends with `/`, so nothing already written reads differently.

**Stream resume.** A parts dir whose manifest predates v2 (no `scan_root` / `all_pivot_names` / `implied`) **finalizes as v2**, not v1: the scan root is a function of the call (`canonical(scheme://bucket)`), and every pivot column the parts hold is written with nothing implied (whether a class was single-valued was decided by the stream pass that produced the parts, which the old manifest does not record). A stage line says so. Refusing to resume was the alternative; finishing in v2 costs nothing and the manifest's own fields are enough.

**Reader seam.** `blobfs.read_parquet` (every engine blob read: parquet and hybrid storage `load`/`get_path_stats`/delete rewrites, `shallow`, `scans show`, and now `migrate*`) returns the v1 shape for a v2 file: `uri` is derived, implied columns are restored, and the column order is v1's. `columns=['uri', …]` works on either format. The cost is one extra footer read per call on a remote blob (a local footer read is negligible). A frame read this way and saved again goes back out as v2 through `write_listing`, so a v2 file's scan root travels with the data through every rewrite. One reader intersected a wanted column list with a blob's *raw* schema names (`shallow.chunk_top_rows`' sidecar-less fallback), which dropped `uri` from a v2 chunk's projection and served `uri: null` for chunk tops; `listing_format.readable_columns(schema)` (the schema's columns plus the derived ones, in v1 order) is the seam for that, and it is the one such reader.

**JS readers.** hyparquet decodes only Snappy natively (`parquet unsupported compression codec: ZSTD`). The site Functions (`_lib/index.ts`, `api/bench.ts`) and the static-deploy reader (`ui/cfn/parquet.ts`) now pass `compressors` backed by `fzstd@0.1.1`, which is pure JS (no wasm, which Workers can't compile at runtime). They are ready for the flip. Snappy files decode as before. Deploy order for the flip: **the site must ship before any job writes zstd index parquet**. The D1 footer (`index_footer.py`) already stores the codec per row group and passes it through.

#### Reader audit

| reader | columns used | new format |
|---|---|---|
| `storage/parquet.py`, `storage/hybrid.py` (`load`, `get_path_stats`, delete rewrite) | everything, incl. `uri` via callers | `blobfs.read_parquet` restores `uri` + implied; `save` and the rewrites write v2 via `write_listing` |
| `shallow.chunk_top_rows` (sidecar-less fallback: projected chunk read) | the columns `/api/scan` emits, incl. `uri` | intersects with `listing_format.readable_columns`, not the raw schema |
| `server.py` `/api/scan` (ancestor-scan branch masks on `df['uri']`; children JSON carries `uri`), `/api/compare`, `/api/histogram`, `/api/filter`, delete | `uri`, `path`, `parent`, sizes | via storage `load` → restored |
| `cli/snapshots.py` (publishes `uri` in `tree.parquet`) | `CORE_COLUMNS` incl. `uri` | via `backend.load` → restored; the published tree stays v1 (public contract) |
| `cli/du`, `cli/filter`, `cli/histogram`, `cli/diff`, `cli/vocab`, `filter.py`, `histogram.py`, `sidecar.py` | `path`, `size`, `kind`, `mtime`, `depth`, … | via `load` (restored) or an explicit `path/size/kind` projection: never `uri` |
| `diff_index.load_scan_table`, `diff.py` chunk refs | `path, parent, depth, kind, size, n_desc, n_children, mtime`; `child_scan_id` | unaffected (`read_table` projection) |
| `shallow.py`, `cli/scans.py` | whole blob / depth-1 | `blobfs.read_parquet` → restored |
| `cli/migrate.py` (`migrate`, `migrate-hybrid`, `migrate-blobs`) | whole blob, rewritten | was `pd.read_parquet`, now `blobfs.read_parquet`, so a rewrite keeps the scan root; rewrites go out through `write_listing` (v2, 64K-row groups) |
| `blobfs.rewrite_row_groups` (`migrate-row-groups`) | batches, schema metadata | metadata and the file's own codec are kept |
| `access/state.live_bucket` | root row's `uri` | v2: `disk_tree.scan_root` |
| `find/tiers.write_tiers` | `SELECT *` | tiers inherit the v2 columns + format keys; switch codec |
| `find/groups.write_groups` | footer | records the codec (`ZSTD`) |
| `storage/sqlite.py`, `storage/duckdb.py` | `uri` column in their tables | fed in-memory v1 frames by the pandas engines, not layer-2 files: n/a |
| `sqla/model.py`, `scan_manifest.py` | Scan rows only | n/a |
| `cloud/cascade_a2a.py` | `path, usr, size, n_files, sum_storage_class_id_{2,3,4}, mtime_mean` | an implied class pivot = `size` (it used to be compared against 0) |
| `cloud/index.py` path-index / coarse / age index / age pyramid | `path, depth, kind, size, n_files, mtime_mean, mtime` | unaffected: **byte-identical** outputs (test) |
| `cloud/sweep.py` `build_manifest` / `build_expiry_manifest` (`plan-sweep-manifest`, expiry) | `path, size, mtime, kind` | unaffected: **byte-identical** manifests (test) |
| `cloud/viz.py`, `cloud/listing.py`, `cli.py` layer-1 paths | layer-1 `storage_class_id`, not layer-2 | n/a |
| `cloud/overtime.py` | path-index rows | n/a (writes the switch codec) |
| site Functions `_lib/index.ts` (path-index, tiers, age, over-time via D1 footers), `api/bench.ts` | index columns | + zstd `compressors` |
| `ui/cfn/parquet.ts` (r2.rbw.sh static reader of scan blobs) | `BASE_COLS` (already excludes `uri`) + `mtime_mean`, `child_scan_id` | + zstd `compressors`; v2 fixture test |
| site `/files` viewer (`FilesPage.tsx` → `@rdub/file-tree` parquet renderer) | any (generic table) | decodes zstd since file-tree `dist.cef98b1` (`defaultCompressors`, `fzstd`; a `compressors` option for more codecs) |
| site `/v1/files` proxy | raw bytes | n/a |
| `packages/*` | none (no parquet readers) | n/a |

No reader needs `uri` materialized: every one either restores it through the seam, uses the metadata's scan root, or never reads it.

#### Verification

- Both suites below run under **both codecs** (a `codec` fixture sets `$DISK_TREE_PARQUET_CODEC`, and asserts the footer codec matches).
- `cloud/tests/test_listing_slim.py`: from a v1 listing (the v2 file's restored view written as the old writer did: DuckDB COPY, Snappy, 64K-row groups, no metadata) and from the v2 listing of the same labeled, single-class input, `write_index` (path-index, 3 coarse tiers, 8 age-pyramid bins), `write_age_index`, `build_manifest` and `build_expiry_manifest` are **byte-identical**. `cascade_a2a.compare` returns the same report (and fails without the implied-class change).
- `tests/test_listing_format.py`: both engines write the same v2 columns, metadata and codecs (one class → implied; two classes, or one class plus NULLs → written). Both restore to exactly the pandas engine's v1 frame, including column order. Engine tiers from v1 vs v2 layer-2 read back **row-for-row** identical (their bytes differ: no `uri`, extra keys).
- `tests/test_listing_writers.py` (both codecs): `aggregate` + `ParquetBackend.save` per root scheme (the eight roots above) → v2 with that `scan_root`, and `read_parquet` returns exactly `df.to_parquet` → `pd.read_parquet` of the saved frame (columns, order, dtypes, values), `uri` column literal; a mocked-gfind local `index` → `HybridBackend.save`; `import -e pandas` with one class (implied) and two (written); a chunked hybrid save (root `/test`, chunk `/test/large`) followed by the three delete-rewrite shapes, each still v2 with exact `uri`s; the v1 fallback (mismatching `uri`: stderr line asserted verbatim; no `uri`; no root row: silent); `readable_columns` on v1 / v2 / projected schemas; the stream resume of a pre-v2 manifest is **byte-identical** to a fresh v2 run of the same (two-class) input.
- The existing engine parity suites now read outputs through `blobfs.read_parquet` (restored v1 view) and still assert pandas == duckdb == stream.
- JS: `site/functions/_lib/zstd.test.ts` decodes a zstd overlay path-index (and shows that decoding fails without `compressors`). `ui/cfn/tests/parquet.test.ts` reads a v2 zstd `uri`-less blob row for row as the v1 fixture.

Size on a synthetic single-class fixture (200k objects in 400 runs × 10 steps × 50 shards, 208k layer-2 rows; `tmp/`-only script): v1 7.48 MiB → zstd-3 alone 48.7 % → without `uri`/class, Snappy 62.7 % → **v2 34.2 %** (2.56 MiB). That fits the 23 % measured on real cw rows above; synthetic names compress less well.

#### Open (phase 1 follow-ups)

- ~~Flip `$DISK_TREE_PARQUET_CODEC` to zstd~~ Done 2026-09-30 (the default; see the codec switch above).
- The stream engine's intermediate parts still carry `uri` (disk during the run only; dropping it there is a separate, unmeasured saving).
- The shallow sidecar (`.shallow.parquet`) keeps its v1 columns including `uri` — it is not a listing (chunk-local `path`s under many roots) and is small.
- zstd 6/9 write-CPU vs size not measured. `fzstd` decode CPU per 8k-row index group in a Worker not measured.

## Phase 2: recompress history (per deployment, lossless)

A one-off Batch job per deployment (not the laptop — `ec2-data-node` rule) rewrites each old listing in place as new-format (drop `uri` and the trivial class columns, zstd), with verify-then-swap:

1. Write the new file beside the old one.
2. Check row count plus an order-insensitive digest of `(path, size, mtime, kind)`.
3. Swap.

Also update the R2 mirror, or stop mirroring raw listings to R2 if nothing served reads them (check `/files`, `/v1/files`). cw first (≈ 200 GiB saved); gcs's `listing/` is gcs's call.

### Phase 2: the base half, as implemented (2026-09-29)

The rewrite is a base CLI, `disk-tree recompress PATH…` (`src/disk_tree/recompress.py`, CLI `cli/recompress.py`), so each deployment's job is a thin invocation. PATHS are files, dirs (recursive `*.parquet`; sidecars and kept `.v1.parquet` files skipped) or fsspec URLs (`gs://…`, `r2://…`, `s3://…`) through `blobfs`. Per file, streaming by row group — memory is one decoded *source* row group (pyarrow's `iter_batches` reads a group at a time: ≈ 262K rows on the cw files above, up to 1M on the oldest blobs — a few hundred MB, never the file) plus one ≤64K-row output batch:

1. **Analyze**, one projected pass over `path, uri, size, sum_*, mtime, kind`: the scan root is derived from the first row (`uri` at `.`, else `uri` minus `/path`) and **checked on every row** — a `uri` that is not `<root>/<path>` (or a file without `uri`/`path`) makes the file not a listing this tool understands, and it is refused untouched (exit 1, reported). A `sum_*` column of `size`'s type that equals `size` on every row (no NULLs) becomes `implied`; the rest are copied. The digest (below) is folded in the same pass.
2. **Write** the kept columns (v1 order minus `uri` and the implied) to `<file>.v2.tmp` beside the original, `pq.ParquetWriter` under `$DISK_TREE_PARQUET_CODEC` (Snappy unless opted in — the same flip rule as phase 1), `row_group_size=65536`, the phase-1 keys (`listing_format=2`, `scan_root`, `implied`, `columns` = the v1 column list). Existing keys stay; a pandas key (the local-scan writers) forgets the dropped columns; `ARROW:schema` is regenerated.
3. **Verify**: the temp's row count and digest must equal the original's, and its parsed format keys the planned ones; otherwise the temp is deleted, the original untouched, and the file reported (`VerifyError`).
4. **Swap**: `os.replace` locally; `fs.mv` (copy + delete) on a URL; `-k/--keep` first moves the original to `<stem>.v1.parquet`. A `.groups.json` beside the blob is regenerated (its offsets describe the old groups). A vocab sidecar goes stale (it indexes row groups; `disk-tree vocab` refuses stale reads) — rebuild it.

**Digest.** Order-insensitive: the sum mod 2⁶⁴ of `pd.util.hash_pandas_object` (a 64-bit per-row hash) over `(path, size, mtime, kind)`, hashed from the Arrow values (not the file's pandas metadata, which the rewrite edits) one batch at a time — memory is one batch, never the file. Every other column is copied verbatim, batch for batch, so the digest columns are the four every consumer keys on rather than the whole row. `tests/test_recompress.py::test_digest_is_order_insensitive` pins the property.

**Report.** One line per file (`old → new (ratio%, rows, root, implied…)`, `already v2`, `would rewrite`) and a totals line; `-j/--json` emits `{results, failures, totals}` with byte sizes and ratios. `-n/--dry-run` runs the analysis (so it reports root + implied) and writes nothing. Already-v2 input is skipped (idempotent). `disk-tree listing-format PATH…` is the audit that finds the v1 files: version (`v1 | v2 | not-a-listing`), codec, row groups, rows, size, root + implied, from the footer only (`-j`).

**Verified** (`tests/test_recompress.py`, 15 tests, both codecs): a v1 fixture in the old writers' shape (DuckDB COPY Snappy with no metadata, and `to_parquet` with a pandas key) → v2 (keys, columns, codec, 64K groups from a 1M-row-group original), reads back through `blobfs.read_parquet` as *exactly* the original frame (`assert_frame_equal`, dtypes included), smaller; two real class pivots stay; a chunk blob without a `.` row; a corrupted verify digest leaves the original byte-identical with no temp; v2 input skipped; `--dry-run` writes nothing; `--keep` leaves a byte-identical `.v1.parquet` that a dir walk never picks up again; a non-listing and a mixed-root file are refused untouched; the same over a `memory://` dir. Fixture (1021 rows, 20 dirs × 50 files, synthetic names): DuckDB-written v1 Snappy 15,253 B → v2 Snappy 11,609 B (76 %), v2 zstd 7,068 B (46 %); pandas-written v1 Snappy 22,029 B → 14,611 B (66 %) / 10,070 B (46 %). Footer-dominated at this size; the real-file ratios are the ones measured above (≈ 15–23 %).

**Against a real bucket (2026-09-30, from cw's first Batch run).** `recompress gs://…` hung: the Batch task printed nothing for 2 h and was killed at `maxRunDuration`; a `-n` on node `mgu` printed its report and never exited (`Task was destroyed but it is pending! … PrefetchProducer._loop`). Root cause: gcsfs 2026.8.1 (a fresh `uv sync` resolved it; the lock had 2026.7.0, which streams a 4 GiB listing and exits cleanly) turns its "adaptive prefetching" reader on by default (`USE_EXPERIMENTAL_ADAPTIVE_PREFETCHING`), so every read handle owns a producer task on fsspec's loop — and a handle pyarrow still held at interpreter exit (`ParquetFile` ↔ `PythonFile` is a cycle, so only the final GC frees it) was finalized after that loop was gone, blocking forever in `fsspec.asyn.sync`. Fix, both halves: `blobfs` sets the process default to off (a footer seek + sequential row groups is what a readahead cache is for) and `blobfs.open_read` names the cache explicitly; `recompress`/`listing-format` read every URL source through it under `_opened`, which `close(force=True)`s before returning (`ParquetFile.close()` alone never closes a caller-supplied source). `test_url_source_handles_name_a_cache_and_are_closed` pins both, over `memory://`; the 10 MB `hero-checkpoints` v1 listing on GCS reproduced the hang under 2026.8.1 and exits cleanly with the fix. cw's job stages via `google-cloud-storage` meanwhile, which is also fine.

**Open (the deployments' half):** the per-deployment Batch job invoking `recompress` over `cw-l2/…` and gcs's `listing/` (`listing-format` first, to size the v1 set), the R2 mirror question, zstd on or off for the rewrite (`$DISK_TREE_PARQUET_CODEC`, gated on the `/files` viewer per phase 1). Nothing here has run against a real bucket.

## Phase 3: cross-scan consolidation — superseded by `path-store.md`

This phase planned an object-level SCD-2 archive of the raw listings. Since `path-store.md` (2026-09-30), the served store holds every object row, and the raw listing is being retired (path-store §4.6). So the archive this phase described is the path store's consolidation: path-store §4.7 (one archive for both sorts, a factor-2 reverse-chronological build ladder capped by a read-amplification budget, zero-decode row-group stitching shared with pyrmts, and optional exponential thinning as a per-deployment retention policy). Nothing further is built under this spec; phases 1–2 (the v2 format, `recompress`, zstd, the R2 dedup) stand as implemented.

## Out of scope

- (Was: "consolidating the indexes". With objects in the store, consolidating the store is the only consolidation left; it lives in `path-store.md` §4.7.)
- gcs `access/` logs (2.43 TiB): a separate retention question for the gcs session.
