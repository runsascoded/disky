# Diff index: full scan-pair diffs, computed once, served from parquet

## Problem

`/api/compare?recursive=1` is an interactive best-first walk (`recursive_diff`, spec `diff-and-search.md` §3a): one directory listing per expansion, ~0.2 s each on a hybrid home-dir scan, capped by `budget` (UI: 200). Consequences the user hits:

- **Cold cost every time**: ~45 s at the root before the treemap colors in; the in-memory `_cache` is per-process and per-`(uri, scan pair, budget)`, so a restart or a drill to a new subpath pays again.
- **Frontier is budget-cut, not depth-cut**: 3,257 of the root walk's rows were `pruned` (queued but never expanded). Their cells can only say "Δ not localized (walk budget)". Drilling to one re-walks from that subpath — correct but another cold wait, and its own budget.
- **No overnight prep**: the project's premise is "always-ready index" (scans run on cron), but diffs are only ever computed on demand.

Enlarging `budget` doesn't fix this: the walk's cost is per-expansion I/O; a *complete* diff of a home dir is tens of thousands of expansions (hours). The walk is the right tool for "where is the Δ" with a small budget; it is the wrong tool for a full diff.

## Proposal

Two-tier: serve the first request as today, and in the background build a **full diff index** for the scan pair — vectorized, not walked — persisted like a scan. Every later request for that pair (any subpath, any depth) is a parquet slice.

### 1. Full diff, vectorized

`diff_index(scan_a, scan_b) -> DataFrame`:

1. Load both scans fully (`backend.load(blob, follow_refs=True)` — hybrid chunks expanded), columns `path, kind, size, n_desc, n_children, mtime`.
2. Outer-merge on `path` → per-row status: `added` (only in b), `removed` (only in a), `changed` (size or n_desc differ), `touched` (equal size & n_desc, mtime differs), `unchanged` (drop).
3. Prune to the frontier the walk would produce with infinite budget: drop rows whose ancestor is `added`/`removed` (the ancestor row is the whole story); keep everything else — a `changed` dir *and* the changed rows below it (the tree needs both for the treemap's spines).
4. Emit `DeltaRow` columns plus `depth`, `parent`, `size_delta`, `n_desc_delta`, sorted `(depth, path)` — the same physical layout as scans, so `StorageBackend.load(path_prefix=, min_depth=, max_depth=)` pushdown works unchanged.

Two full loads + one merge on ~1–3 M rows: tens of seconds, once, in a background thread — vs. hours of walk.

Per-parent `unchanged` context (`unchanged: {top, rest}` in today's response) is a by-product of the same merge: group the `unchanged` rows by parent, keep the top-8 by size, aggregate the rest. Store as a second table (or a `unchanged_rank` column on kept rows + a `rest` sidecar keyed by parent).

### 2. Storage

- Parquet at `~/.config/disk-tree/diffs/<scan_a>-<scan_b>.parquet` (+ `…-unchanged.parquet`).
- SQLite table `diff`: `id, scan_a, scan_b, time, blob, status (running|done|failed), n_rows, n_added, n_removed, n_changed, n_touched, total_delta, error`. Unique on `(scan_a, scan_b)`.
- A `DiffProgress` row (or reuse `ScanProgress` with a `kind` column) so the existing SSE progress stream can report it.

### 3. Serving

`/api/compare?recursive=1`:

1. If a `done` diff index exists for the pair → slice: rows under `uri` (path-prefix pushdown), rebased to `uri`, up to a **row budget** (not an expansion budget): take rows ordered by `(depth, -|Δ|)`, stop at N (e.g. 20k), and mark the last kept depth's dirs `pruned` only where the slice was cut. Nothing is ever "not descended" — it's all in the index; `pruned` becomes "response trimmed, drill to see more" and is exact.
2. Else → today's walk (unchanged), and enqueue the index build if not already running. Response gains `index: {status: 'building'|'done'|'none', started, rows_so_far}`.

`/api/diff/status?scan1&scan2` (or an SSE event on the existing progress stream) reports index state. **Progressive fill in the UI**: `CompareView` keeps the walk result on screen; when the index reports `done` it refetches the same request (now served from the index, sub-second) and re-renders — pruned cells resolve in place, without the spinner.

### 4. Scheduled prep

- `disk-tree diff-index SCAN_A SCAN_B` (CLI) and `disk-tree diff-index --latest [PATH…]`: for each path, index the two most recent scans.
- `disk-tree sync` (the cron entrypoint) runs `diff-index --latest` after importing new scans, so the "what changed since last night" view is instant in the morning.
- GC: `disk-tree index --gc` drops diff indexes whose scans are gone.

### 5. Non-goals / open questions

- Not replacing the walk: it stays the cold-path fallback and the only option when an index isn't built yet (e.g. comparing two arbitrary historical scans).
- Memory: two full home-dir scans in pandas is a few GB peak; run one build at a time (a lock like the scan queue), and consider DuckDB for the merge on the duckdb backend.
- Row budget vs. treemap: with the index, the UI could instead request `max_depth` + `min_abs_delta` (a real filter) rather than a row cap — cheaper and deterministic. Decide when wiring the client.

## Status

Implemented 2026-08-28 (`src/disk_tree/diff_index.py`, `cli/diff_index.py`, server `/api/diff/status` + index-backed `/api/compare?recursive=1`, UI poll-and-refetch):

- Build is Arrow-native (scans loaded as Arrow tables with chunks expanded — ~1 GB each vs ~4 GB as pandas — then a full-outer join *per depth*, since rows only ever match at their own depth). Home dir, 6.8M ↔ 7.4M rows: ~4 s load + ~12 s diff, 1.33M index rows, 59 MB. Depths stream to parquet as they finish (`iter_diff_depths`), so only one depth's join output is live at a time: peak RSS 7.3 → 3.8 GB, same bytes out.
- Serving: root slice 1.5 s (row budget 20K by |Δ| + ancestor spines, `pruned` marks dirs with trimmed descendants), `c/oa` 0.3 s, a leaf dir 30 ms. Before: 45 s / 24 s / 2 s walks.
- Unchanged context is stored in the index (`context=True` rows: unchanged siblings of changes), so the response shape is unchanged.
- `disk-tree index` and `disk-tree sync`/`pull` build the index against the path's previous scan when a scan lands (`-D` skips); `disk-tree diff-index -a` backfills every path's latest pair.
- `disk-tree diff-index -g` drops indexes whose scans are gone plus orphan parquets (`-n` to preview); `Scan.gc` (i.e. `index --gc`) calls it after deleting scans.
- Lazy per-drill serving: the client sends `min_frac` measured from its own canvas (`MIN_CELL_PX / canvas_px` — a node's screen area is ≈ its byte share × the canvas), and drilling *inside* the widget fetches that subtree's slice (`hasChildren`/`loadChildren`), so deep cells the root view trimmed come back at the subtree's own scale. Root response at a 1400×340 map: 2,811 rows / 1.33 MB (vs 4,115 / 2.13 MB at the fixed default, 20,194 / 15.3 MB untrimmed). Dirs with no fetchable slice — unchanged (no index rows) and added/removed (stored as one row by design) — navigate to their own page instead of drilling into a blank map.
- Request-side trimming (`serve_slice`): `min_frac` (default 2e-5) drops rows whose bytes *and* |Δ| are both under that fraction of the compared subtree — a cell that small can't be drawn at any sane canvas size — plus `max_depth`, with `max_rows` as the backstop. Thresholds are relative to the *compared* subtree, so drilling into a `pruned` dir resolves detail the parent view couldn't draw. Home-dir root: 20,194 → 4,115 rows, context 16,315 → 1,530, response 15.3 → 2.1 MB, 1.7 → 0.9 s; `Library/Caches` drilled is 2.0 MB.

Interim walk fixes that also landed: `touched` status, chunk-aware flat compare, ancestor-scan fallback in `disk-tree diff`, 64K-row parquet row groups (`migrate-row-groups`), Arrow-side chunk map, batched parallel walk, aligned-join child comparison, single-flight `/api/compare` — the walk itself went 45 s → 5 s at the root.
