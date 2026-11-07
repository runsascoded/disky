# mgu ↔ disk-tree engine audit (2026-09-07)

Written from the mgu session (`~/c/oa/marin-gcs-usage`, branch `gcs`) after a module-by-module inventory of what mgu implements itself against what the engine has. Companion to `mgu-scale-unification.md` (the asks) — this is the *why* and the full map. Short version: mgu consumes four engine symbols and one CLI, carries its own copy of the rollup, the index tiers, the footer/blob sync and the Worker reader, and vendors a `src/disk_tree` of which most is unused.

## What mgu actually imports

- `disk_tree.access.aggregate.aggregate_access`, `disk_tree.access.parsers.parser_for`, `disk_tree.access.parsers.gcs.{DEDUPE_WARN_FRACTION, dropped_fraction}`, `disk_tree.access.read_sizes.aggregate_read_sizes` (`gcs_usage/access.py`, `reactive.py`)
- `disk-tree bulk-list` (the CLI, from the Batch task body — `gcs_usage/batch.py`)
- `disk_tree.listing.prepare_listing` (as of today; mgu's own copy, which upstream ported 2026-08-04, is deleted)

`tree_build.build_tree` — the module mgu pushed upstream in `5be454c` — no longer has a caller in mgu: `tree.json` is gone (view-serving), so it's now upstream-only value.

The vendored `src/disk_tree` is near parity (`access/**` byte-identical; `find/aggregate_duckdb.py` 13 lines apart; `server.py`/`diff.py`/`config.py` stripped) and lacks `blobfs.py`, `sidecar.py`, `scan_manifest.py`, `diff_index.py`, `extents.py`, `library.py`, `desktop.py` and their CLIs.

## Module classes

(a) mgu product logic, stays · (b) engine-shaped, implemented in mgu · (c) already upstream.

| mgu module | what | class | engine counterpart |
|---|---|---|---|
| `viz.py` (`webdata`) | DuckDB cascade `dir_stats → dir_attr → dir_agg → ptu` per `(path, depth, usr)`, class pivots `c2/c3/c4`, exact `wts` (DECIMAL(38,0)), access max `a`; floor-free `path-index.parquet` + `-by-user` sort (8k-row groups); `write_coarse_tiers` (E ∈ 16/20/24, floor `2^(round(log2 fleet) − E)` in KV metadata) | **b** | `find/aggregate_duckdb.py`, `find/aggregate_stream.py`, `find/agg_ext.py` (`--pivot-sum`, `--mean-mtime`), `cli/snapshots.py` |
| `index_footer.py` | footer → compact revivable JSON per row group (`rg_json` + depth/path/bytes/usr min-max); D1 sync with generations; `<tier>.groups.json` group-manifest blob; gc/retire/compact | **b** (extraction + blob) / a (D1) | `sidecar.py` (second artifact beside a blob), `scan_manifest.py` + `ui/cfn/manifests.ts` (pointer beside a remote blob) |
| `stage.py` | parallel GCS → local-NVMe prefetch of parquet inputs (glob split, fnmatch, workers) | **b** | `blobfs.py` is the single-object seam; no stager |
| `attr_index.py` | row-group-pruned point reads of the path index for the sweep gate | b (mechanics) / a (semantics) | `storage/parquet.py`, `storage/base.py::path_prefix_bounds` |
| `access.py`, `reactive.py` | incremental usage-CSV ingest (watermark / set-difference drain), L1a + L2a shards | b (driver) / a (layout) | `cli/access.py` has import/agg/top, no incremental loop |
| `batch.py` | GCP Batch fan-out: one task per bucket shelling `bulk-list`, reuse landed shards | a / b (pattern) | `cli/sync.py` (cron on one host) |
| `prefixes.py` | attribution prefixes, deepest-prefix lookup | a (content) / b (join) | `mgu-scale-unification.md` §B `--label` |
| `cli.py` `index-{tiers,sync,blob,gc,dir,compact}`, `webdata`, `stage` | the (b) commands | b | — |
| `sweep_*.py`, `mark.py`, `digest*.py`, `identity.py`, `rules.py`, `wandb_*.py`, `signals.py`, `records.py`, `executor_info.py`, `healthcheck.py`, `gcp.py`, `usernames.py` | marks ledger, sweep, Slack, attribution mining, site checks | a | — |
| `job/run.sh` | the daily sequence: ingest ‖ list → barrier → stage → webdata → publish → index-sync → healthcheck → digest → index-gc | a, with the (b) skeleton `stage → aggregate → tiers → publish → sync metadata → gc generations` | `cli/sync.py` |
| `site/functions/_lib/index.ts` | Worker reader: D1 handle, `reviveRowGroup`, `groupMatches`, `readRects`/`readAsks`, blob handle | b | `ui/cfn/parquet.ts` (footer stats + `prefixBounds` + range reads) |

## Engine modules mgu doesn't use but overlaps

| engine | mgu's own | overlap |
|---|---|---|
| `find/aggregate_stream.py` (O(depth) streaming du, depth-partitioned finalize) | none — `viz.py` hash-aggs 179M dirs | **vendored and unused**; exactly the shape mgu's `webdata-memory.md` prescribes |
| `find/agg_ext.py` | `viz.py` pivots + `wts` | same two features, reinvented |
| `storage/{base,parquet,hybrid}.py` | `write_coarse_tiers` + `attr_index.py` | two answers to "sorted parquet + row-group pushdown as serving substrate" |
| `sidecar.py` + `cli/vocab.py` (name → row-group block index) | `_lib/view.ts` `q=` scans rows | mgu could consume this for server-side name search |
| `diff_index.py` | `api/diff.ts` (live two-scan read) | precomputed vs live |
| `histogram.py` | `viz.py` age strata → `age.json` | materialized vs query-time; §E |
| `blobfs.py` | `stage.py` + ad-hoc fsspec | not vendored in mgu |

## Ranked: what should move, and what blocks each

1. **`viz.py`'s cascade → `find/aggregate_*`** (`mgu-scale-unification.md` §A–B). One rollup, two implementations; mgu's needs a 256 GiB node and OOM-killed the 9/5 daily. Blocked on: file-backed DuckDB + `--partition-depth` (§A); `usr` as a cascade group key via `--label` (§B); from `import-a2a-findings.md`: `n_desc` counts dirs where mgu's `o` counts objects (needs `n_files`), and the `a//b` mis-parenting policy.
2. **Consume `aggregate_stream.py`.** Already vendored; the O(depth) fix for the hash-agg memory. Blocked on §B (no `usr` slices, pivots, `a` in its output) and prefix-contiguous inputs across six buckets' shards.
3. **Index tiers as an engine output** (`--tiers/--coarse-exp/--sort-variant`, floors in KV metadata, 8k-row groups). A generic serving contract ("the coarsest tier whose floor is under the query's pixel threshold"); ~60 lines of mgu SQL today. Blocked on 1 (a tier is a cascade output, not a post-pass); `cli/snapshots.py` assumes one 64K-row-group blob per scan, no floors. Wants `bulk-list`'s `_SUCCESS.json` to carry `started`/`finished` for as-of (§D).
4. **Footer extraction + `<tier>.groups.json` → engine**, next to `sidecar.py`. **Landed (DT, 2026-09-07): `find/groups.py`** — `extract` / `schema_json` / `group_rows` / `groups_json` / `write_groups`, mgu's `index_footer.py` wire format verbatim (envelope `{v, version, schema, floor_bytes, groups}`, group arrays in `index_row_groups` column order, `rg_json = [num_rows, codec, [[data_page_offset, total_compressed_size, dictionary_page_offset|0]…]]`); the size column resolves `size` (DT tiers) then `b` (mgu's path index), `usr` bounds when the tier has one, the floor from `floor_bytes` (DT) or `coarse_floor` (mgu) KV metadata. `write_tiers(groups=True)` / `disk-tree import --tiers … -G/--groups` writes one beside every tier, local or URL (`blobfs`). D1 sync / generations / gc stay mgu-side, as asked. Tests `tests/test_groups.py`: exact group spans over a 5000-row objects tier at 2048-row groups, `usr` slice bounds, the `b` fallback, the document == `groups_json(extract(tier))`, a `file://` write. "Precompute the footer as a compact side artifact so a serverless reader never parses thrift cold" is engine-shaped (a fine tier's footer cannot be parsed in a Worker: 27k groups ≫ 128 MB). Blocked on: the wire format (`[num_rows, codec, [[data_page_offset, total_compressed_size, dictionary_page_offset]…]]`, group array `[rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json]`) is joint-owned with mgu's `reviveRowGroup`/`groupMatches`; D1 (generation pointer, `INSERT OR REPLACE` flip, gc/retire/compact) stays mgu-only and must split cleanly from the extraction.
5. ~~`listing.py`~~ — done today: mgu imports `disk_tree.listing.prepare_listing` (upstream's is a superset: S3 Inventory).
6. **Access plane: hour grain + `--as-of` + per-scan state** (§D). Blocked on the `day DATE → hour TIMESTAMP` schema change (mgu re-aggregates its retained raw shards) and a live-dirs tier (3) for `dt access state` to semi-join.
7. **`_lib/index.ts` ⟷ `ui/cfn/parquet.ts` convergence.** Two Cloudflare hyparquet range readers over `(depth, path)`-sorted parquet; mgu's is the superset (D1/blob handles, lens variants, `groupMatches`, rect batching). Blocked on the row schemas (`{path, depth, usr, b, o, wts, wb, c2..c4, a}` vs `{path, size, mtime, kind, parent, n_desc, n_children, depth}`) — downstream of 1.
8. **`stage.py` → engine, beside `blobfs.py`.** Parallel prefetch to local NVMe; gcsfuse is 20–50 MB/s and the aggregation makes four passes. Blocked on nothing structural; upstream's workloads haven't needed it.

Direction: 1–3 are the unification spec, which is the gate for everything mgu is waiting on (objects tier, `/api/age`, object marks). 4, 7, 8 are engine-shaped code mgu will keep growing on its side until the cascade output unifies; the ask is only that DT own the *formats* (tier floors in KV metadata, the group-manifest wire format) so the two readers can converge. 5 is done; 6 rides on 3.
