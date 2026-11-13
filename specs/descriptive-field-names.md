# Descriptive field names: store columns, D1 and the site API

**Status:** Phase 1 (inventory) and Phase 2 (site API renames) done on `w/field-names`, 2026-11-12, but for `/api/name-summary` (deferred, §Phase 2 Built). Phase 3 is a plan. Owner: the root `disky` session (`cloud`). Implementation: a subagent on `w/field-names` in `wt/field-names`.

## Why

Short names (`us`, `wb`, `wts`, `vf`/`vt`, `c2..c4`, and the map API's `k b o d a cb f`) date from an early design that golfed JSON bytes on the wire. Today they cost readability everywhere and save almost nothing:

- **Parquet** stores a column name once, in the footer. A short column name saves nothing.
- **D1** column names are schema, not per-row bytes.
- **The site's JSON** repeats keys per node, but responses are compressed (brotli/gzip at Cloudflare's edge), which removes most of the repetition. At 10⁴–10⁵ nodes (a full-window treemap at a 16 px² minimum cell), the remaining cost is browser parse time over the uncompressed bytes. The fix for that is a **columnar** body (one array per field), not short keys.

One rename is already done: gcs's `c2`/`c3`/`c4` became `nearline_bytes`/`coldline_bytes`/`archive_bytes` in the generation backfill rebuild (2026-10-18). hf names its own classes (`private_bytes`, `git_bytes`).

## Phase 1: inventory (commit it into this spec first)

Every short or opaque name, per surface, with: where it's defined, every reader and writer (site functions, `site/src`, `fleet/`, `packages/*`, `dt-cloud` Python incl. probe/warm-cache/digest/diff-bench/layout-sim, job scripts, tests, fixtures), its meaning, and a proposed descriptive name. Surfaces:

1. **Served-store columns:** interval store sorts (`path`, `bysize`, `slices`, `level`, `attribution`, …), static-names files, per-scan path-index sorts, sidecars (`.groups.json` keys like `b_min`/`b_max`/`p_min`), `scans.json`/`meta.json`/manifest keys.
2. **D1 tables and columns** (`site/schema.sql`, and each deployment branch's schema where it differs).
3. **Site API JSON:** map/subtree, diff, series, search/filter, level, staged, scans, admin, fleet. Node keys (`k b o d a cb f us`, …) and top-level keys.
4. **URL query params** are out of scope (they're user-visible URLs; renaming breaks links). List any that look worth it separately, no changes.

Names that are already clear (`depth`, `path`, `size`, `mtime`, `n_files`, …) stay. A name stays short only if the inventory gives a reason (e.g. a key a third party reads).

## Inventory (Phase 1, 2026-11-12)

### Proposed renames at a glance

- **Site API (Phase 2):** map node `n k b o t a us cb c f m pv src at` → `name kind bytes objects mtime last_read owners class_bytes children folded matched provenance store scan`, and `d` (epoch day, always beside `t`) leaves the wire; diff row `p d k s a b oa ob x l` → `path depth kind status bytes_before bytes_after objects_before objects_after expanded lookup` (+ top-level `total_a/_b`, `objects_a/_b` → `bytes_before/_after`, `objects_before/_after`); the `{b, o}` pair → `{bytes, objects}` on series points, filter-cover items/roots, prefix stats, `/api/owners` users (`mix` → `class_bytes`), name-summary hot views.
- **The one choice for the root:** `bytes`/`objects` (proposed: the UI's words, the spec's example, `/api/owners`'s top level) vs the store's `size`/`n_files`. The root picked `bytes`/`objects`.
- **Columnar:** measured before any code (below, §Phase 2): 10–20% fewer compressed bytes, no client-time win once decoded, and a decode step in every consumer (the server's union merge reads member bodies; six Python readers). Proposed not to ship it; the root agreed: not shipped.
- **Store (Phase 3, at a rebuild):** only the interval store's `path`-sort columns are safe from the rethink: `vf`/`vt` → `valid_from`/`valid_to`, `us` → `owner_slices`, `wts`/`wb` → `mtime_weighted_sum`/`mtime_weight_bytes`, `c2..c4` → class names via `CLASS_COLS`, `churn_cum`/`churn_end` → `cumulative_churn`/`…_at_end`; `scans.json`/manifest key collisions (`version`, `level`, `generation`). Everything else in 1a/1b is retire?; per-scan sorts and sidecars retire.
- **D1 (Phase 3, recipes):** the footer tables (`index_row_groups`: `rg d_min p_min b_max u_min rg_json`, `index_schema`: `date gen dir version`) belong to the layout lean-scans retires: leave them. Worth recipes: `admin_edits`, `og_tokens`, `scan_runs`, `activity_*`, `deletion_progress` short names; `deletion_runs.head`/`exec_head` dropped. Auth-package and pyrmts tables stay.

### 1. Served-store files

Abbreviations: py = `cloud/src/dt_cloud/`, `ct/` = `cloud/tests/`, `_lib/` = `site/functions/_lib/`, `fx/` = `_lib/fixtures/`. Reader lists are from grep (a few may be false positives). Any column rename regenerates the binary fixtures (`fx/iv*`, `fx/static-*`, `fx/v2*`, `fx/dups`).

**What the index rethink would retire** (the "retire?" column):
- `specs/lean-scans.md` (partly built): the per-scan `path`/`bysize`/`-by-user` sorts, their `.groups.json`/`.groups.parquet` footers, the D1 footer rows and the search sidecars become transient; per-scan `attr.tsv` moves into the interval store's `attribution` table. Kept per scan: `meta.json`, `age.json`, the age pyramids (until an interval age index), `ck.txt`, over-time. → **retire**.
- `full-scan-serving-bench.md` (`wt/full-scan-bench`): design A, one in-RAM depth-first pass over the interval store's `path` sort, would answer every view without per-axis indexes, making `bysize`, `slices*`, `level`, `reads` and the static name index redundant if adopted. The `path` sort's columns are its input. Undecided. → `path`: **keep**; the rest **retire?**.
- `fleet-index-research.md` (`wt/index-research`): the current interval/static indexes stay the serving baseline; its leading candidate (shared segment text/topology + presence discovery + separate historical measures) would replace both wholesale if chosen. Not built. → reinforces **retire?** on everything but the `path` sort's inputs.

So: rename the interval store's `path`-sort columns at a rebuild; leave the rest until the rethink decides; never touch per-scan sorts and sidecars.

#### 1a. Interval store (`interval-store/<gen>/served/<sort>.parquet` + `.groups.parquet`, runs under `deltas/…`)

Writers of every served column: py `interval_store.py` (`PV_SCHEMA`/`SV_SCHEMA`/`SVT_SCHEMA`, `path_rows_sql`, `build_range`, `fold_sql`, `slice_totals_sql`, `cut_gen`/`write_served`), `interval_append.py`, `interval_compact.py`, `interval_backfill.py`, `interval_local.py`. **IS-core readers:** py `interval_read.py`, `interval_verify.py`, `interval_export.py`, `layout_sim.py`, `level_sim.py`; TS `_lib/index.ts` (`IV_ROW_COLUMNS`, `toRowV3`); tests `ct/test_interval_*.py` (~12), `test_parallel_cut`, `test_served_pages`, `test_cut_budget`, `_lib/intervalStore.test.ts` + ~20 IV tests; `fx/iv*/gen.py`; off-branch `wt/full-scan-bench` (`bench/full-scan/src/*.rs`, `oracle/*.py`).

| sort | name | meaning | proposed | extra readers | retire? |
|---|---|---|---|---|---|
| all (`path`, `bysize`, `reads`, `slices*`, `attribution`) | `vf` / `vt` | version valid from / to: scan epoch s, half-open; `vt` = 4291747200 while open | `valid_from` / `valid_to` | + `interval_attribution.py`, `interval_churn.py`, `sweep_source.py`; `_lib/diffDescent.ts`, `diffLevel.ts`, `view.ts`, `ownerHist.ts`, `api/series.ts` | `path`: keep; others retire? |
| `path`, `bysize` | `us` | owner slices in one string: `''`, an owner, or JSON `[["usr", bytes], …]` | `owner_slices` | `interval_churn.py`; TS `_lib/index.ts` only (decoded to `Row.us`) | as above |
| `path`, `bysize`, `slices*` | `wts` | Σ mtime_mean · weight | `mtime_weighted_sum` | `_lib/view.ts`, `pathStore.test.ts` | as above |
| same | `wb` | Σ weight bytes behind the mean | `mtime_weight_bytes` (the static index's `mtime_w`) | `_lib/view.ts` | as above |
| same | `c2` `c3` `c4` | class 2/3/4 bytes (`CLASS_COLS`, `IV_CLASS_COLS`); gcs already renamed its own (`nearline_bytes`…) | `class_2_bytes`… generically, or the deployment's names through `CLASS_COLS` | `_lib/index.ts`, `diffLevel.ts`, `testStore.ts`, `fx/gen.py` | as above |
| `slices`, `slices-bytotal`, `slices-bysize-user` | `usr` | the slice's owner (NULL = unowned) | `owner` | `_lib/ownerBands.ts`, `ownerHist.ts`, `ownerTotals.ts`, `prefixes.ts`, `scope.ts`, `view.ts`, `api/series.ts` + ~8 tests | retire? |
| `slices-bytotal` | `tot` | the path's total size at that version (sort key) | `path_total_size` | `_lib/view.ts`, `sliceTotals.test.ts`, `sliceComplete.test.ts` | retire? |
| `path`, `bysize` (churn gens) | `churn_cum` / `churn_end` | cumulative churn at `vf` / at `vt` (closed only) | `cumulative_churn` / `cumulative_churn_at_end` | `_lib/index.ts` `IV_CHURN_COLS`, `diffLevel.ts`, py `interval_level.py`, `layout_sim.py`, `level_sim.py`; `fx/iv-churn` | `path`: keep; else retire? |
| `level` | `depth`, `path` | the **parent's** depth and path | `parent_depth`, `parent_path` | `_lib/index.ts` `IV_LEVEL_COLS`, `diffLevel.ts`, `api/diff/level.ts`, py `interval_level.py`, `level_sim.py`, `layout_sim.py` + tests | retire? |
| `level` | `ts` / `op` / `churn_prev` | transition's scan epoch / 0 appeared, 1 changed, 2 gone / cumulative churn at the previous transition | `scan_ts` / `change` / `cumulative_churn_prev` | same | retire? |
| range dirs (builder-only) | `pv/` `pvl/` `rd/` `sv/` `svt/` `digest/` `sv-digest/` | path versions, + last read, read intervals, slice versions, + totals, digests | low value | py `interval_*` | retire? |
| `served/*.groups.parquet` (`GROUPS_SCHEMA`) | `rg`, `seg` | row group; dyadic segment (0 = open versions) | `row_group`, `segment` | `_lib/index.ts` `IV_FOOTER_COLS`, py `interval_read.py`, `interval_verify.py`, `interval_export.py`, `layout_sim.py`, `level_sim.py`, `sweep_source.py` | retire? |
| same | `d_min/max` `p_min/max` `b_min/max` `u_min/max` `vf_min…vt_max` `rg_json` | group bounds (depth, path, size or `tot`, owner, versions) + `[num_rows, codec, [[data_off, size, dict_off]…]]` | `depth_min`… `path_min`… `size_min`… `owner_min`… `valid_from_min`… `row_group_meta` (same names as D1's `index_row_groups`, §2) | same + `fx/iv/gen.py` | retire? |

JSON keys (writers py `interval_local.py`, `interval_append.py`, `static_names.pick_scans`, `interval_store.record_*`, `interval_churn.record_churn`, `interval_level.record_level`; readers TS `_lib/index.ts`, `staticRuns.ts`, `staticDrill.ts`, `nameSummaryStatic.ts`, `health.ts`, `aheadCost.ts`, `manifests.ts`, `footerCache.ts`, `og/serve.ts`, `_middleware.ts`, `data/[[path]].ts`; ~25 py modules):
- `scans.json`: `scans[].ts` → `scan_ts`; `scans[].version` (the source listing format, colliding with the object-version column and `index_schema.version`) → `listing_format`; `scans[].size` (source sort bytes, a pin) → `src_bytes`; `scans[].generation` (GCS pin, colliding with the top-level `generation` flag) → `src_generation`; top-level flags (`slices`, `churn`, `level`, `generation`, `attribution`, `object_version`) → optionally `has_*`. `ranges.json` `k` → `n_ranges`.
- `manifests/<id>.json`: `stamps` → `scan_ts`; `date` → `newest_scan`; `runs[].level` and a run's `meta.json` `level` (the binary counter's, colliding with the `level` sort/flag) → `counter_level`; `runs[].first`/`last` → `first_scan`/`last_scan`.
- `CURRENT.json` (`{"gen"}`): clear.

#### 1b. Static name index (`static-names/<gen>/`) — all retire?

Writers py `static_names.py`, `static_catalog.py`, `static_drill.py`, `static_roots.py`, `static_anchors.py`, `static_append.py`, `static_compact.py`, `static_merge.py`, `static_local.py`. Readers: those + `anchor_profile.py`, `static_measure.py`; TS `_lib/staticNames.ts`, `staticCatalog.ts`, `staticDrill.ts`, `staticAnchors.ts`, `staticFilter.ts`, `staticRuns.ts`, `nameSummaryStatic.ts`, `testBrute.ts`, `api/static-bench.ts`; 10 `_lib/static*.test.ts`, 17 `ct/test_{static,anchor,drill}_*.py`, 7 `fx/static-*`.

| file | name | meaning | proposed |
|---|---|---|---|
| intervals, `sx/`, `names/`, roots, `cdelta` | `usr` | owner (`''` = none, NOT NULL, unlike the interval store's) | `owner` |
| same + catalog cells, rollups | `vf` / `vt` | valid from/to: epoch s (intervals, roots), **timestamp(ms)** (`sx/`, `names/`); a cell's change time (catalog, rollups); rollup **header** `vf` = kept children (overloaded) | `valid_from`/`valid_to`; `changed_at`; header `kept_children` |
| intervals | `c2..c4`, `mtime_w` | class bytes (hardcoded in `VALUE_COLS`), mean-mtime weight | as the interval store's |
| `sx/`, `names/` | `s` | lowercase suffix key (`names/`: `/` + name) | `suffix` / `name_key` |
| catalog cells, drill roots/rollups, anchors rollups | `q` | the search literal (anchors: suffix or `/`+name) | `literal` / `key` |
| catalog cells, rollups | `b` / `o` | Σ size / n_files of first hits; **header** rows: range or root rows / cell or child count (overloaded) | `size` / `n_files`, header fields split out |
| rollups | `kind` (int8) | row type −1 delta header, 0 header, 1 kept child, 2 remainder (not file/dir) | `row_kind` |
| sidecar/index/top files | `rg`, `s_min/max`, `q_min/max`, `k_min/max`, `p_min/max`, `chunks` | row group; bounds of suffix/literal/key/path; flat `[data_off, size, dict_off]` per column | `row_group`, `suffix_min`…, `literal_min`…, `key_min`…, `path_min`…, `column_chunks` |
| drill `meta.json`, `anchors/meta.json` | `R`, `K`, `rg`, `idx_rg` | read bound rows, kept children per heavy dir, data / index row-group rows | `read_bound_rows`, `kept_children`, `row_group_rows`, `index_row_group_rows` |
| `shards.json` | `i`, `lo`, `hi` | shard number, key-prefix bounds | `shard`, `prefix_lo`/`prefix_hi` (low value) |
| drill `aliases.parquet`, `<kind>-fresh.parquet` | `n` | root rows / fresh-root count | `root_rows` |
| builder-only | `ubytes`, events `t`/`db`/`dn`, digests `n`/`h1`/`h2`/`d1`/`d2`/`dn` | decoded bytes; event time, Δsize, Δn_files; hash sums and deltas | low value |

#### 1c. Per-scan path index and sidecars

Writers py `viz.py` (`write_path_index`, `snapshot_meta`, `age_rows`), `index.py`, `index_parts.py`, `src/disk_tree/find/tiers.py`, `find/groups.py`, py `extras.py`, `dups.py`.

| file | name | meaning | proposed | readers | retire? |
|---|---|---|---|---|---|
| `path-index[-bysize][-by-user].parquet` (v2) | `usr`, `tot`, `sum_storage_class_id_2..4` | owner slice; path total (sort key); class bytes | `owner`, `path_total_size`, `class_2_bytes`… | `_lib/index.ts` `V2_ROW_COLUMNS`, `view.ts`; py `interval_store.py`, `static_names.py`, `interval_verify.py`, `cascade_a2a.py`, `dups.py` (prunes by `tot`), `index_parts.py`, `find/{tiers,groups,tier_plan}.py` | **retire** (`dups` then needs another source for `tot`) |
| v1 legacy (`interval-store export` still writes v1) | `b o wts wb c2..c4 a` | v1 wire names | the v2 names | `_lib/index.ts` `V1_ROW_COLUMNS`, `api/path-index.ts`; py `interval_store.py`, `interval_verify.py`, `interval_export.py`, `cascade_a2a.py`, `cli.py` | **retire** |
| `<sort>.groups.json` / `.groups.parquet` | `rg d_min/max p_min/max b_min/max u_min/max row_start/row_end rg_json`; kv `groups_v` | as 1a's footers; format version | as 1a; `groups_version` | `_lib/index.ts` `FOOTER_COLS`, `footerCache.ts`, `search.ts`; `find/{groups,tier_plan,search}.py`, py `index_footer.py`, `bysize_check.py` + tests | **retire** |
| `part.json`, `roots.parquet`, `age.parquet` (work dir) | `partial.attr`; `b o wts wb c2..c4 a own_o`; `d1`, `a` | transient intermediates | v2 names, `own_objects`, `top_dir`, `last_read_day` | `index_parts.py` | transient (low value) |
| `age.json` rows | `d d1 u a b o` | listing day, top dir, user, last-read day, bytes, objects | `day top_dir user last_read_day bytes objects` (`age.parquet` already says `day`/`user`/`bytes`/`objects`) | `site/src/types.ts` `AgeRow`, `AgeChart.tsx`, `App.tsx`, `federatedViews.ts`, `rowSelection.ts`; `_lib/restrict.ts`; py `index_verify.py` | keep |
| `age-pyramid-<bin>.parquet` | `b`, `o`, `binstart` | bytes, objects, bin start (ms) | `bytes`, `objects`, `bin_start` (check pyrmts's `metrics` contract first) | `_lib/agePyramid.ts`, `api/age-pyramid.ts`; py `index.py`, `age_store.py`, `index_footer.py` | keep until an interval age index |
| snapshot `meta.json` | `users[].u`/`.b`, `access.from`/`to` | user, bytes; epoch days | `user`, `bytes`; `from_day`/`to_day` | `site/src/types.ts` `UserInfo`/`Meta`, `UserPage.tsx`, `Allowances.tsx`, `App.tsx`, `federatedScans.ts`, `stores.ts`, `digest/cw.ts`; `_lib/series.ts`, `api/series.ts`; py `digest*.py`, `index_verify.py` | keep |
| `attr.tsv.idx.json` | `v`, `n` | format version, line count | `version`, `lines` | `_lib/extras.ts`; py `extras.py`, `interval_attribution.py` | **retire** |
| `dups.parquet` / `dups.json` | `tier`, `rel`, `v`, `by_pair[].b1`/`b2`, `check_n`, `check_eq` | `tree`/`moved` match kind; path relative to the bucket; format version; the bucket pair; spot checks run / equal | `match`, `rel_path`, `version`, `bucket_a`/`bucket_b`, `checks_run`, `checks_equal` | `_lib/dups.ts`, `api/dups.ts`, `site/src/dupsModel.ts`, `fx/dups/gen.py`; py `dups.py` | keep |

Cross-cutting collisions: `version` means three things (`scans.json` listing format, `index_schema.version`, the object-identity column); `level` two (the served sort/flag, a run's counter level); `vf`/`vt` are seconds in the interval store and static roots but ms timestamps in static `sx/`/`names/`.

### 2. D1 tables and columns

From `site/schema.sql` (cloud) and each deployment branch's. Paths are under `site/functions/` unless noted; py = `cloud/src/dt_cloud/`.

**Keep (a third party owns them):** `@open-athena/auth` (its `dist/adapters/d1.js`) reads and writes `access_log` (`ts`, `ua`, `bucket` = `ts/3600`, `session_sub`, `as_org`), `access_log_daily` (`day`, `clients`), `access_requests`, `allowed_emails` (`who`, `ts`), `grants`, `pending_auth`, `profiles`, `session_log` (`sid`, `via`, `build`, `ua`, `n_events`/`n_errors`/`n_batches`, `last_ms`), `session_log_batches` (`sid` per tab, `pid` per page load, `n`), `session_log_switch` (`who`, `ts`). A rename there starts in that package. `pyrmts` owns `pyramid_multiscans` (`tier`, `shard_dur`, `key`; py `overtime.py` writes, `_lib/overTime.ts` reads). **Every table the admin DB console shows** has its column list in `_lib/tables.ts` (served by `api/db/[[path]].ts`, drawn by `site/src/AdminDbPage.tsx`/`DbTable.tsx`): a rename updates it too.

**Index footers (retire?: the layout the index rethink may replace; rename together with the sidecars `.groups.json`/`.groups.parquet`, §1):**

| table | column | meaning | proposed | writers | readers |
|---|---|---|---|---|---|
| `index_row_groups` | `date` | scan id | `scan` | py `index_footer.py` (`index-sync`); `_lib/footerCache.ts` | `_lib/index.ts`, py `index_footer.py`, `scan_retire.py`, `src/disk_tree/find/groups.py` (+~19 tests) |
| | `gen` | generation id | `generation` | same | same |
| | `rg` | row-group ordinal | `row_group` | same | `index.ts` (`FOOTER_COLS`), py `cli.py`, `scan_retire.py`, `static_drill.py`, `static_roots.py`, `groups.py` |
| | `d_min`/`d_max` | group's `depth` range | `depth_min`/`depth_max` | same | `index.ts`, `groups.py` |
| | `p_min`/`p_max` | group's `path` range | `path_min`/`path_max` | same | `index.ts`, `index_footer.py` (byte estimate SQL), `groups.py` |
| | `b_max` | group's max size (`tot`/`size`): the bytes floor | `size_max` | same | `index.ts` (`bFloor`), `groups.py` |
| | `u_min`/`u_max` | group's `usr` range (NULL for age) | `owner_min`/`owner_max` | same | `index.ts` (lens pruning), `groups.py` |
| | `rg_json` | `[num_rows, codec, [[data_off, size, dict_off]…]]` | `row_group_meta` | same | `index.ts` (`reviveRowGroup`), `index_footer.py`, `groups.py`; `diffLookups.test.ts` matches the SQL text |
| `index_schema` | `date` / `gen` | scan id / generation | `scan` / `generation` | py `index_footer.py`, `age_store.py`, `interval_store.py`, `overtime.py`, `publish.py`, `scan_steps.py` | `index.ts`, `scanArg.ts`, `scanTimes.ts`, `health.ts`, `stores.ts`, `dups.ts`, `data/[[path]].ts`, py `gen_gc.py`, `scan_retire.py`; `site/src/HealthPage.tsx`, `healthModel.ts` (+~22 tests) |
| | `dir` | the generation's bucket dir (the pointer) | `generation_dir` | `index_footer.py` | `index.ts`, py `gen_gc.py`, `index.py`, `publish.py`, `scan_retire.py`, `scan_steps.py`, `groups.py` |
| | `started` | the scan's start instant | `started_at` | `index_footer.py` (`index-sync -m`; backfill SQL in `cli.py`) | `index.ts`, `scanArg.ts`, `scanTimes.ts`, `data/[[path]].ts`; `site/src/App.tsx`, `scan.ts`, `scanSlug.ts`, `healthModel.ts` |
| | `version` | listing layout (1 = dir-only wire names, 2 = …) | `layout_version` | `index_footer.py` | `index.ts`, py `index.py`, `interval_store.py`, `scan_retire.py`, `groups.py` |
| `footer_cache` | `date`/`gen`/`rows` | scan / generation / `index_row_groups` rows held | `scan`/`generation`/`row_group_count` | `footerCache.ts`; py `index_footer.py` | `footerCache.ts`, `index.ts`, `api/footer-cache.ts`, py `cli.py`, `scan_retire.py` |

**Owned tables, other short names:**

| table | column | meaning | proposed | writers | readers |
|---|---|---|---|---|---|
| `owner_prefix_aggs` | `idx` / `body` | index tag `mode:gen` / JSON prefix cache | `index_tag` / `prefix_cache_json` | `_lib/ownerTotals.ts` | same |
| `owner_prefix_hist` | `idx` / `hz` / `versions` | index tag / scan ordinal priced through / JSON `[vf, vt, agg][]` (agg keys `b o us uo cb`) | `index_tag` / `priced_through` / `versions_json` | `_lib/ownerHist.ts` | same |
| `activity_bursts` | `via` `origin` `counts` `pages` `sids` `trail` `dirty` | `session\|grant\|public`; site origin; JSON `{kind: n}`; JSON top 20 paths; JSON session ids; JSON `{n, last[]}`; render pending | `auth_via` `site_origin` `kind_counts_json` `top_pages_json` `session_ids_json` `page_trail_json` `render_pending` | `_lib/activity.ts` | `activity.ts`, `api/activity/[[path]].ts`; `site/src/ActivityPage.tsx` |
| `activity_events` | `via` / `line` | as above / rendered one-line summary | `auth_via` / `summary_line` | `activity.ts` | same |
| `activity_kinds` | `log` `notify` `who` `ts` | log on/off; `off\|count\|line`; editor; time | `log_enabled` `notify_mode` `updated_by` `updated_at` | `activity.ts` (`setKind`) | `activity.ts`, `site/src/ActivityKinds.tsx` |
| `activity_posts` | `part` | `parent` or a line kind's reply | `post_part` | `activity.ts` | same |
| `admin_edits` | `tbl` `pk` `who` `ts` | table edited; its key; editor; time | `table_name` `row_key` `edited_by` `edited_at` | `activity.ts`, `annotations.ts`, `plans.ts`, `sweepSettings.ts`, `api/admins.ts`, `api/db/[[path]].ts`, `api/digest-settings.ts`, `api/session-log/[[path]].ts`, `api/sweep/settings.ts` | `api/activity/[[path]].ts`, `site/src/Admins.tsx` |
| `admin_emails` | `who` / `ts` | who added the row / when | `added_by` / `added_at` | `api/admins.ts`, `slack/actions.ts`, py `admins.py` | `_lib/auth.ts`, `tables.ts`, `site/src/AdminPage.tsx` |
| `agent_tokens` | `created` | mint time | `created_at` | `api/token.ts` | same |
| `chart_annotations` | `at` / `created` / `updated` | scan id the note sits on / times | `scan` / `created_at` / `updated_at` | `_lib/annotations.ts` | same; `site/src/chartNotes.ts`, `NoteWidgets.tsx`, `SizeOverTime.tsx` |
| `digest_reapply` | `out` / `ts` | output log URI / time | `output_uri` / `requested_at` | `api/digest-reapply.ts` | same |
| `og_tokens` | `kind` `view` `page` `exp_day` `minted_ts` `revoked_ts` | card kind; view key; page URL; expiry epoch day; times | `card_kind` `view_key` `page_url` `expires_day` `minted_at` `revoked_at` | `_lib/og/tokens.ts`, `stagedSlack.ts` | `tokens.ts`, `api/og/[[path]].ts`; `site/src/PreviewLinks.tsx` |
| `deletion_runs` | `head` / `exec_head` | old ledger positions, always 0 since the marks demolition | **drop** (not rename) | `planDispatch.ts`, py `sweep_exec.py` | `sweepReflect.ts`, `stagedSlack.ts`, `tables.ts`, py `cli.py` (+~20 tests) |
| `deletion_progress` | `ts` / `done` / `bytes_exact` | snapshot epoch s / bucket pass finished / bytes exact | `snapshot_at` / `bucket_done` / `bytes_are_exact` | py `sweep_progress.py` | `_lib/runProgress.ts`, `og/run.ts`, `og/runData.ts` |
| `scan_dispatch` | `who` `job` `ts` | claimer; Cloud Run job; time | `claimed_by` `cloud_run_job` `claimed_at` | `_lib/scanDispatch.ts`, `api/scan-runs/dispatch.ts` | `api/scan-runs/[[path]].ts`, `tables.ts` |
| `scan_runs` | `kind` `parent` `spot` `gen` | run kind; orchestrating run; SPOT VM; generation | `run_kind` `parent_run_id` `is_spot` `generation` | py `scan_runs.py`, `scan_steps.py`, `scan_head.py`, `maint_runs.py`, `scan_runs_backfill.py` | `_lib/scanRuns.ts`, `health.ts`, `og/serve.ts`, `og/policy.ts`; `site/src/ScansPage.tsx`, `scanRunsModel.ts`, `healthModel.ts` |
| `scan_run_outputs` | `key` / `gen` | output name / generation | `output_key` / `generation` | py `scan_runs.py` | `scanRuns.ts` |
| `stage_replies` | `stager` / `open` | staging user / still collecting (partial unique index) | `staged_by` / `is_open` | `_lib/stagedSlack.ts` | same |
| `plan_items` | `as_of` | scan the item was staged against | `staged_at_scan` | `_lib/plans.ts`, py `staged_plan.py` | `dispatch.ts`, `api/plans/[[path]].ts`; `site/src/batches.ts` |

Clear enough to keep: `rendered_ms`, `claimed_ms`, `msg_id`, `first_ts`/`last_ts`, `row_start`/`row_end`, `floor_bytes`, `deletion_bands.gone`/`overwritten`, Slack's `*_ts`. Repeated low-value patterns, only if wanted: `ts` → `*_at`, `who` → `*_by`, `gen` → `generation`.

**Deployment branches:** `cw`, `cw-dev`, `cw-prod`, `hf` match cloud. `hf-*`, `r2*` only lack `scan_dispatch` (`hf-prod` also `session_log.grant_id`/`subject_name`). Branch-only short names:
- **gcs** (`gcs`, `gcs-dev`, `gcs-prod`), read by shared code: `actions` (`set_owner`, `set_keep`, `keep`, `memo`, `ts`; `keep`/`set_keep` likely vestigial since the marks demolition; `api/actions.ts`, `_lib/actionLog.ts`, `_lib/ledger.ts`), `owner_prefixes.tombstoned` → `retracted_at`, `owner_totals` (`head` → `ledger_head`, `body` → `totals_json`, `claims` → `claims_json`, `ms` → `compute_ms`), `claims` (`who`, `ts`), `user_emails.user` → `owner_id` (`_lib/me.ts`, `view.ts`, `og/pages.ts`, `api/assignments.ts`), `session_monitor` (`checked_s`/`signal_s`/`alert_s` → `checked_at`/`last_signal_at`/`last_alert_at`), `deletion_runs.plan` (the run's manifest dir) → `run_dir`.
- **m3 / local:** `hosts` (`home`, `root`, `mark` (unclear), `store`, `os`), `agents.seen_ts` → `seen_at`.

### 3. Site API JSON

Sampled live from the r2 demo (`https://r2.rbw.sh/api/…`, 2026-11-12) and read from the code. "Readers" lists the consumers that must change with the key; every route's tests (`*.test.ts` beside it, `functions/gates.test.ts` shapes) assert bodies and change with them.

**Naming choice (for the root):** the map's two measures appear on ~every body (`b`, `o`). Proposed: `bytes` / `objects` (the words the UI and the spec's own example use; `size` reads as either). Alternative: the store's `size` / `n_files` (one name across store, `largest` and the API). The diff's two sides: `_before` / `_after` (the diff is `prev` → `curr`; `a`/`b` collide with the map's `b` = bytes today).

#### 3a. Map node (`/api/subtree` `tree`, nested; also `unionRoot`, `stagedTree`, `/user` estate)

Defined: server `site/functions/_lib/view.ts` (`ViewNode`, `nodeOf`, `display`, `build`), `_lib/union.ts` (`unionRoot`: `src`, `at`); client `site/src/types.ts` (`TreeNode`). Python mirror: `cloud/src/dt_cloud/interval_read.py` (`interval-store verify`'s reference view, compared key by key in `interval_verify.py`).

| key | meaning | proposed | readers beyond the definitions |
|---|---|---|---|
| `n` | node name (last path segment; the root's label) | `name` | 22 `site/src` files using `TreeNode` (`App.tsx`, `Treemap.tsx`, `spine.ts`, `ChildrenTable.tsx`, `PrefixTable.tsx`, `filterTree.ts`, `ledgerOverlay.ts`, `stagedTree.ts`, `UserPage.tsx`, `OgPage.tsx`, …); `_lib/og/*`, `_lib/union.ts`; py `probe.py`, `digest_cw.py`, `interval_read.py`/`interval_verify.py` |
| `k` | `file` \| `dir` | `kind` | same set as `n` |
| `b` | bytes at or under the node | `bytes` (alt `size`) | same set; py `probe.py`, `bench/score.py`, `sheet_mirror.py`, `diff_bench.py`, `interval_read.py` |
| `o` | objects at or under the node | `objects` (alt `n_files`) | same set; py `bench/score.py`, `interval_read.py` |
| `d` | newest write, epoch **days** (`floor(t / 86400)`) | **drop** from the wire (always beside `t`); the client derives the day (`mtimeDay(n)`) where ~10 call sites read it. Alt: `mtime_day` | `ChildrenTable.tsx`, `PrefixTable.tsx`, `Treemap.tsx`, `readLens.ts`, `viewNums.ts`, `filterTree.ts`, `stagedTree.ts`, `AgeChart.tsx`, `ObjectPanel.tsx`; `interval_read.py` |
| `t` | newest write's instant, epoch s | `mtime` (the store column it is the max of) | `Treemap.tsx`, `viewNums.ts`, `readLens.ts`; `interval_read.py` |
| `a` | last read, epoch days (subtree max, access logs) | `last_read` (store column name) | `ChildrenTable.tsx`, `PrefixTable.tsx`, `Treemap.tsx`, `readLens.ts`, `ledgerOverlay.ts`, `stagedTree.ts`, `AgeChart.tsx`; `interval_read.py` |
| `us` | `[owner, bytes][]`, desc | `owners` (the root's pick: the UI, `/api/owners` and the filters all say owner) | `ChildrenTable.tsx`, `PrefixTable.tsx`, `OwnerBar.tsx`, `filterTree.ts`, `ledgerOverlay.ts`, `stagedTree.ts`, `StagedPage.tsx`, `Treemap.tsx`, `types.ts` (`userBytes`); server `_lib/owners.ts`, `ownerBands.ts`, `ownerHist.ts`, `prefixes.ts`, `stagedSlack.ts`, `og/data.ts`, `og/pages.ts`; `interval_read.py` |
| `cb` | non-Standard class → bytes (`"2"` NL, `"3"` CL, `"4"` AR) | `class_bytes` (as `meta.json`'s) | `DupsPage.tsx`, `stagedTree.ts`, `OwnerControls.tsx`, `Treemap.tsx`, `types.ts` (`classMix`); `interval_read.py`/`interval_verify.py` |
| `ou` (gcs, picked 2026-11-13) | bytes whose ownership was never read (a static drill without owner slices, an unread union member) | `unknown_owner_bytes` | `staticDrill.ts`, `union.ts`, `og/data.ts`, `OwnerBar.tsx` |
| `c` | children | `children` | everything walking the tree; py `probe.py`, `digest_cw.py` |
| `f` | an `(other)` fold: how many children it stands in for | `folded` | `Treemap.tsx` (dust density); `interval_verify.py` (`with_f`) |
| `m` | `1`: the node matches the name filter | `matched` (boolean) | `filterTree.ts`, `Treemap.tsx` |
| `pv` | `[source, evidence, prefix]` provenance of the top owner | `provenance` | `ChildrenTable.tsx`, `spine.ts`, `OwnerControls.tsx` |
| `src` | union root child: its store | `store` | `unionSources.ts` |
| `at` | union root child: that store's scan id at the cutoff | `scan` | `unionSources.ts` |

Top level (`/api/subtree`): `date path w h minArea atten tier index lens threshold nodes truncated owner q matches matched matchCount matchesCapped rollup excluded firstPaint interiors partial partialReason approximate approximateReason dirsOnly hexRuns budgetCut union tree`. Clear enough; `w`/`h` echo the request's params (keep). `nodes` is a count: `node_count` would free the name for a columnar body (§Phase 2). Mixed camel/snake is pre-existing (`minArea`, `total_a`); not worth churning.

#### 3b. Diff row (`/api/diff` `rows`, flat)

Defined: `view.ts` (`emit`, `capDiffRows`), client `site/src/diffModel.ts` (`DiffRow`), `diffRows.ts`. Readers: `diffModel.ts` `buildTree`, `DiffTable.tsx`, `diffRows.ts`, `_lib/diffDescent.ts`, `api/diff.ts` (union merge); py `diff_check.py` (`dt-cloud diff-check`, `diff-bench`'s CASES), `diff_bench.py`.

| key | meaning | proposed |
|---|---|---|
| `p` | path (relative to the request's `path`) | `path` |
| `d` | depth | `depth` |
| `k` | `file` \| `dir` | `kind` |
| `s` | `added` \| `removed` \| `changed` \| `unchanged` | `status` |
| `a` / `b` | bytes at `prev` / `curr` | `bytes_before` / `bytes_after` |
| `oa` / `ob` | objects at `prev` / `curr` | `objects_before` / `objects_after` |
| `x` | expanded: its children are rows too | `expanded` |
| `l` | `1 \| 2`: found by a lookup (which side) | `lookup` |

Top level: `total_a total_b objects_a objects_b` → `bytes_before bytes_after objects_before objects_after` (same pair names as the rows); `prev curr expansions truncated threshold lookups lookups_capped …` stay.

#### 3c. Level (`/api/diff/level`, paused; no `site/src` consumer)

`_lib/diffLevel.ts` (`Sides`, `LevelRow`, `Level`): `a b oa ob` as the diff's; `p` → `name` (a child's name, not a path), `k` → `kind`, `s` → `status`; request params `p`/`k`/`m` are URL params (§4). Top-level response echoes `k`, `m` → `top_k`, `measure`. Readers: `api/diff/level.ts`, `unionLevel.test.ts`, py `level_sim.py`/`layout_sim.py` (they price reads, not bodies; check).

#### 3d. The `{b, o}` pair elsewhere

| body | where | short keys | proposed |
|---|---|---|---|
| `/api/series` `points[]` | `api/series.ts`, client `series.ts`, `packages/react` `TimeSeries` callers; py `overtime.py`, `age_bench.py`? | `b`, `o` | `bytes`, `objects` |
| `/api/age-pyramid` `records[]` | `api/age-pyramid.ts`, `_lib/agePyramid.ts`; client `AgeChart.tsx`, `ageBars.ts` | `dt`, `b`, `o` | `time`?, `bytes`, `objects` (`dt` is pyrmts's record key: check the lib before renaming) |
| `/api/filter-cover` `items[]`, `roots` | `_lib/cover.ts` (`CoverItem`), `api/filter-cover.ts`; client `filterCover.ts`, `coverTargets.ts`, `MatchActions.tsx` | `b`, `o`; `roots.n` | `bytes`, `objects`; `roots.count` |
| `/api/name-summary` hot views | client `hotModel.ts`, `hotTreemap.ts` (`HotMapNode`) | `b`, `o` | `bytes`, `objects` |
| `meta.json` (`/data/meta.json`) `users[]` | store file (§1), client `types.ts` `UserInfo`, `identityRegistry.ts` | `u`, `b` | `user`, `bytes` (rides a meta rewrite: Phase 3) |
| `age.json` rows (`AgeRow`) | store file (§1), client `types.ts`, `AgeChart.tsx` | `d d1 u a b o` | `day top_dir user last_read bytes objects` (Phase 3) |
| `/api/owners` `users{id: {b, mix}}` (top level already `bytes`, `objects`) | `api/owners.ts`; client `UserPage.tsx`; py `sheet_mirror.py` (gcs's deployed sheet-mirror image) | `b`, `mix` | `bytes`, `class_bytes` |
| `/api/prefixes` stats (`PrefixStat`: a node's measures, no children) | `_lib/prefixes.ts`; client `prefixes.ts`, `PrefixTable.tsx`, `StagedPage.tsx`, `stagedTree.ts` | `b o d a us cb` | as 3a (`d` → derived from `mtime`, which it would then carry) |
| owner pages (`UserPage.tsx` `OwnerCell`) | client-built from `/api/subtree` | `n b c` | follows 3a |
| fleet `/api/fleet` (`fleet/functions/_lib/fleetTypes.ts`) | `fleet/src/Fleet.tsx` | `MetaPoint.t`, `FleetMeta.d1`/`d7` | `time`, `day_ago`/`week_ago` |

**Deploy skew.** The site and the API's Python readers ship separately: `probe`, `warm`, `digest_cw`, `bench/score`, `diff_check`/`diff_bench` run from job images, `sheet_mirror` is its own deployed image, `interval_verify` compares against a live site. So the Python readers accept both spellings (new first) in the rename commit, and the old spelling is dropped once every deployment's site and images carry it. The browser: `API_CV` (client) + `CACHE_V` (edge) move together; an open tab on the old bundle gets new bodies until it reloads.

Bodies that are D1 rows passed through (admin, runs, plans, staged, grants, activity, session-log, usage) carry D1's column names: they change only with §2's renames.

### 4. URL query params (out of scope; no change)

`site/src/urlParams.ts` is the registry (~100 params, each with a description shown on the help page): one- and two-letter keys (`c cl cx d dc dk dm ex f k l li ln lu mm mt mu n nn o p pg ps q qs rb rn …`) are the page's shareable state. Renaming breaks every shared link; the registry already documents each. The only candidates worth a long alias (accepting both, writing the long one) would be the ones people type: none stand out. API-only params (`p`/`k`/`m` on `/api/diff/level`, `w`/`h`/`minArea`/`atten`/`cv`/`ps` on the views) are internal but are cache-key terms; leave them.

## Phase 2: site API (ships on its own)

- Rename node and response keys to their descriptive names, server and client in the same commit(s), with a `RESPONSE_V`/`CACHE_V` bump for every cached body whose shape changes (see the "Bump RESPONSE_V on shape change" rule).
- **Columnar bodies** for the large node lists (map, diff, level): `{ "path": [...], "bytes": [...], "owner": [...], ... }` plus a small decoder on the client. Measure before/after on real responses (gcs root, a depth-6 window, a search): compressed bytes, uncompressed bytes, client parse + decode time. Keep columnar only if it doesn't lose on those.
- Python consumers of the API (probe, warm-cache, diff-bench, digest, …) change in the same commit.
- `functions/gates.test.ts` and every API test assert the new shapes exactly.

### Built (2026-11-12, `w/field-names`)

Three `[cloud]` commits, each with its cache bumps (`CACHE_V` 12 → 13 → 14 in `edgeCache.ts`, the client's `API_CV` 4 → 5 → 6 and `diff_bench.CV` with it; `COVER_V` 8, `MATCHES_V` 2), server, client, Python readers and tests together:

1. **Map nodes** (§3a): `/api/subtree`'s `tree` (`view.ts` `ViewNode`, now a precise interface; the union root's `store`/`scan`), `/api/prefixes`' stats (`PrefixStat` = a node's measures). `d` leaves the wire; the client takes the day from `mtime` (`types.ts` `mtimeDay`). The Python oracle (`interval_read.node`/`tree`/`flatten`, `interval_verify.compare`) emits the same names; `fixtures/iv/expected.json` regenerated on mgu with a new `iv/gen.py expected` mode (expectations only, the store's bytes untouched), checked equal to the old file under the rename (84 views, 468 tiles, each `mtime`'s day the old `d`).
2. **Diff rows and levels** (§3b–c): rows `path depth kind status bytes_before bytes_after objects_before objects_after expanded lookup` (`capDiffRows` over `DiffSides`; the union merge's `src` → `store`), totals `bytes_before … objects_after`; level rows `name kind status` + sides, `other.count`, and the echoed `top_k` / `measure`.
3. **The `{b, o}` pairs** (§3d): `matched` `{ path, bytes, objects }` and `matchCount` `{ count, bytes, objects }` (converted from the readers' own `{ path, b, o }` at `matchLists`), `/api/series` points and root traces (`wirePoint`), `/api/filter-cover` items and `roots`, `/api/name-matches` items, `/api/owners` users (`{ bytes, class_bytes }`, converted from the ledger's stored `{ b, mix }`).

Internal shapes keep their short names where they never reach a body (the view reader's `Agg`, `RootRow`, the ledger's stored totals, the client's table rows): the rename is the wire's.

**Dual-read follow-ups (remove once every deployment's site serves the descriptive names):**
- `cloud/src/dt_cloud/api_names.py`: `node`/`tree` (read by `probe`, `warm`, `bench/score`, `sheet_mirror`, `diff_bench`), `diff` (`diff_check`, `diff_bench`), `owner` (`sheet_mirror`).
- `fleet/functions/_lib/fleet.ts` `SeriesWire`: `bytes ?? b` from each deployment's `/api/series?store=meta`.

**Not done:** `/api/name-summary` (the `/names` page's hot views: `root`, `buckets[]`, `delta` as `b`/`o`). Its `dated-name-summary-v1` contract is validated key-for-key on the client (`nameModel.ts` `weights`, `hotModel.ts`) with its own fixtures (`nameTestFixtures.ts`, `datedNameTestFixtures.ts`, ~200 uses across 16 files); renaming it is a contract change that should come with a `-v2` schema, as its own commit.

### Columnar experiment (measured 2026-11-12, before any code change)

Real bodies from the r2 demo (`https://r2.rbw.sh/api/…`, scan `2026-11-12`; its nodes carry no `us`/`cb`, which gcs's do), each re-encoded three ways by `tmp/colbench/bench.mjs` (worktree-local, not committed): **short** (as served), **descriptive** (the same objects, keys renamed per §3a/3b, `d` dropped), **columnar** (descriptive names, one array per field; the tree flattened preorder with a `depth` column, an absent value `null`, an all-null column omitted). Sizes: raw, gzip-6, brotli-4 and brotli-11 (the edge's level is Cloudflare's choice; the two brotli levels bracket it). Time: median of 40 `JSON.parse` (+ for columnar, the decode back to the nested objects the client draws from) in Node 26 (V8, as Chrome), on an M-series laptop.

| body | nodes/rows | variant | raw KiB | gzip-6 | br-4 | br-11 | parse+decode ms | parse only ms |
|---|--:|---|--:|--:|--:|--:|--:|--:|
| map root 1920×1080, 16 px² | 2,370 | short | 186.9 | 34.3 | 33.4 | 24.1 | 0.45 | |
| | | descriptive | 213.8 | 35.3 | 33.2 | 24.0 | 0.43 | 0.47 |
| | | columnar | 135.7 | 28.1 | 26.5 | 21.9 | 0.45 | 0.20 |
| map root, 4 px² | 5,753 | short | 482.5 | 95.7 | 90.6 | 66.5 | 1.11 | |
| | | descriptive | 548.1 | 96.9 | 90.5 | 66.5 | 1.22 | 1.05 |
| | | columnar | 358.0 | 78.0 | 73.7 | 60.5 | 1.21 | 0.46 |
| map root, 1 px² | 15,598 | short | 1325.7 | 264.0 | 243.5 | 184.3 | 3.31 | |
| | | descriptive | 1491.9 | 266.0 | 243.7 | 184.4 | 3.06 | 2.66 |
| | | columnar | 1003.4 | 214.6 | 207.2 | 169.3 | 3.30 | 1.21 |
| depth-4 window (`ctbk/.dvc/files/md5`), 4 px² | 2,908 | short | 280.5 | 82.6 | 75.6 | 64.9 | 0.60 | |
| | | descriptive | 314.5 | 82.7 | 76.2 | 65.1 | 0.63 | 0.58 |
| | | columnar | 199.6 | 72.9 | 67.4 | 60.8 | 0.66 | 0.25 |
| depth-5 window, 4 px² | 220 | short | 16.8 | 3.3 | 3.1 | 2.3 | 0.03 | |
| | | descriptive | 19.0 | 3.2 | 2.9 | 2.3 | 0.04 | |
| | | columnar | 10.4 | 2.8 | 2.6 | 2.1 | 0.04 | |
| search `q=ctbk`, 4 px² | 1,583 | short | 117.3 | 20.6 | 19.6 | 13.8 | 0.30 | |
| | | descriptive | 133.6 | 20.6 | 19.5 | 13.7 | 0.32 | 0.29 |
| | | columnar | 85.5 | 16.4 | 15.1 | 12.4 | 0.37 | 0.13 |
| search `q=parquet` | 73 | short | 27.0 | 3.4 | 3.3 | 2.8 | 0.04 | |
| | | descriptive | 28.6 | 3.5 | 3.3 | 2.8 | 0.05 | |
| | | columnar | 26.3 | 3.4 | 3.3 | 2.7 | 0.05 | |
| diff `11-11 → 11-12` | 217 rows | short | 23.4 | 5.0 | 4.7 | 3.8 | 0.05 | |
| | | descriptive | 36.3 | 5.3 | 4.9 | 3.9 | 0.06 | |
| | | columnar | 16.5 | 4.3 | 3.8 | 3.5 | 0.06 | |
| diff `11-05 → 11-12`, 4 px² | 612 rows | short | 66.3 | 12.0 | 11.4 | 9.1 | 0.18 | |
| | | descriptive | 103.0 | 12.7 | 12.0 | 9.4 | 0.18 | |
| | | columnar | 49.0 | 10.6 | 9.6 | 8.7 | 0.15–0.23 | |

(`/api/diff/level` isn't measurable on the demo: its generation has no churn, a 409.)

**Reading:**
- **Descriptive keys cost nothing on the wire:** within ±1% compressed on map bodies, +3–6% on diffs (whose rows are almost all keys), at +10–15% raw (map) and +55% raw (diff). Parse time unchanged (±10%, noise).
- **Columnar saves 10–20% compressed** (br-4: −20% map root, −11% depth-4, −16 to −19% diff; br-11: −8 to −12%), ~30% raw.
- **Columnar doesn't save client time.** Parse alone is ~2.2× faster (1.2 vs 2.7 ms at 15.6K nodes), but the decode back into the nested objects every consumer (the treemap, tables, spine, filter tree, staged tree) works on costs it back: parse+decode is a tie (3.3 vs 3.1–3.3 ms at 15.6K nodes; 0.17–0.2 µs a node). Keeping the columns as the client's working form would be a rewrite of every tree consumer.
- **Absolute scale:** ~0.2 µs/node to parse; 10⁵ nodes ≈ 20 ms, against views that take 0.5–8 s to answer. Parse time isn't where a map's latency is.

## Phase 3: store and D1 renames (ride a rebuild; never their own rebuild)

Plan only (not started). Names from the inventory: the interval store's `path`-sort columns `vf`/`vt` → `valid_from`/`valid_to` (epoch seconds), `us` → `owner_slices`, `wts`/`wb` → `mtime_weighted_sum`/ `mtime_weight_bytes`, `c2..c4` → the deployment's class names through `CLASS_COLS`, `churn_cum`/`churn_end` → `cumulative_churn`/`cumulative_churn_at_end`, and the `scans.json`/manifest collisions (`version` → `listing_format`, a run's `level` → `counter_level`, …). Everything the inventory marks retire? waits for the rethink. D1: the footer tables stay (the layout lean-scans retires); recipes for the owned short names in §2.

- **Store columns** change only in a generation that is being rebuilt anyway (the next compaction or backfill, as `c2..c4` did), with the reader accepting both spellings for exactly the generations still served, and that dual-read removed once no served generation has the old names. Each deployment's rebuild is its owner's.
- **D1 column renames** are recipes (`site/migrations/<slug>.sql` + `schema.sql` regenerated, `ALTER TABLE … RENAME COLUMN`), applied by each deployment's owner with Ryan's go.
- **The index rethink** (`specs/full-scan-serving-bench.md`, `wt/index-research`'s `fleet-index-research.md`) may replace the served layout. Don't rename columns of a layout the rethink is about to retire; the inventory marks which.

## Constraints

- Commits are `[cloud]`, generic (no deployment defaults). Send each sha to the root session; root lands it. Don't push `cloud`, `dev` or any deployment branch.
- No tests or builds on the laptop: CI (push `rw/<branch>` to `r` and watch it with `ghws`) or node `mgu` (`scripts/remote-test`).
- No D1 writes, no store writes, no deploys.

## Open

Resolved (root, 2026-11-12): `vf`/`vt` are epoch seconds (a version's half-open `[valid_from, valid_to)`; open at 4291747200), not scan ordinals, and become `valid_from`/`valid_to` at Phase 3; the API says `bytes`/`objects`; a node's owner split is `owners`; columnar bodies don't ship; Python readers dual-read until every deployment carries the rename.

Still open:
- `/api/name-summary`'s weights (`root`, `buckets[]`, `delta`: `b`/`o`) — deferred, see §Phase 2 Built.
- The short-name fallbacks' removal (§Phase 2 Built, follow-ups).
- `iv/gen.py`'s full rebuild (`main`) fails on today's `write_served` (it selects `churn_cum`, which the fixture's ranges don't carry); `gen.py expected` regenerates `expected.json` alone and was used here.
