# Viz prototypes: staleness scatter, violins, treemap age lens, filter plane

2026-08-15, from the "crazy idea for a ~scatter plot" thread. Spun out per Ryan so the main CW-S3 thread doesn't clobber it; a dedicated session/subagent can iterate here. Inputs available today: layer-2 parquet per scan (path, size, mtime, n_files, n_desc, depth; `mtime_mean` + class pivot sums once the site pipeline passes `--mean-mtime`/`--pivot-sum`), access plane agg (per-prefix n_ops/bytes_out by op/hour; GCS only).

## 0. The score: sum-TBy (Ryan's additivity principle — adopted)

Each dir's TB·years score = **Σ over descendant files of `size_i × age_i`** — not `total_size × age(max mtime)` (fsutil's) and not max-anything. Properties:

- **Monoid**: node score = Σ children's scores (dirs + files). Cascades like `size`; treemap-able as an accessor with honest part-of-whole semantics.
- **Already derivable, exactly, from `--mean-mtime`'s internals**: Σ size·age = Σ size·(now − mtime) = now·Σsize − Σ size·mtime = `size × (now − mtime_mean)` (size-weighted mean). The `mt_wsum` HUGEINT partial the engines carry *is* Σ size·mtime — so sum-TBy needs **zero new columns**; it's a query-time derivation per node: `size * (now - mtime_mean) / (1 TiB · 1 year)`.
- **atime mode**: same shape once per-path last-read exists (`dt access` agg: `max(ts)` per prefix; join at whatever prefix depth both sides share). Caveats: only *read* paths have atimes — unread files fall back to `max(logging_start, mtime)`; and access data is per-prefix, not per-file, so atime-TBy is exact only at/below the agg's prefix grain. GCS only (CAIOS has no access logs — probed `NotImplemented` 2026-08-15).

## 1. Log-log staleness scatter ("triage frontier")

- x = age (now − mtime_mean; later: time-since-last-read), y = bytes, log-log; one marker per dir at a chosen depth (or current drill level).
- Iso-score lines: sum-TBy is *not* x·y exactly (x uses the weighted mean, which is what makes it the sum) — but on this plot `y·x = size×mean-age = sum-TBy` exactly, by the identity above. So diagonal iso-lines ARE iso-sum-TBy lines. Upper-right = delete-candidate frontier; draw labeled score bands (0.1, 1, 10, 100 TB·yr).
- Marker size ∝ n_files (the ops/listing-pain channel), hue = top-level parent (Tree Colors: L2 = hue perturbation around parent hue).
- Voronoi-subdivided markers: only for markers above a legibility threshold, or on hover — see §4.

## 2. Violin-per-child ("byte-weighted age distributions")

- x = categorical: children of the current drill dir. y = mtime (later atime). Each child renders its **byte-weighted** distribution of descendant-file ages — violin = mirrored KDE; a plain histogram is the honest v0 (Ryan: "at that point it's more like a histogram" — yes, and that's fine).
- **Area ∝ bytes** (agreed — not n_files, not age). So the violin family is normalized such that each child's total area = its bytes; y-integral above an age threshold = reclaimable bytes at that threshold (slider).
- "Voronoi the violin": the useful version of this instinct is **stacking** — subdivide each child's density by *grandchild* (stacked area / streamgraph inside the violin silhouette), hues linked to the treemap palette. Keeps area-∝ exact per segment, no geometry solver needed.
- Data need: per-child age *histograms*, not just means — a small layer-2 extension (or query-time over layer-2 files at the drill level: bucket file mtimes into ~24 bins × child; cheap in duckdb-wasm/server for one drill level at a time).

## 3. Treemap age lens (H = tree, L/S = age)

Keep hue = top-level parent (existing `tree` mode); modulate **lightness/alpha** by the cell's age (mtime_mean): *older ⇒ more faded* (fades toward panel bg — "fading from memory"). A checkbox lens (like the class-lens hatch), composable with tree AND user hue modes; replaces nothing.

- Channel budget check: H = category, area = bytes, hatch = class lens, dim = user highlight — L is indeed the free channel. Two caveats: (a) faded cells lose label contrast — clamp the ramp (e.g. 100%→45% opacity); (b) L-ramps read poorly across different hues (equal age ≠ equal perceived fade) — use OKLCH lightness, not RGB alpha, if it matters.
- Cheap to prototype in `colorForCell` (site/src/Treemap.tsx): both `mode` and `ageLens` feed bg computation; `dateRange` plumbing already exists from the `date` mode.

## 4. Voronoi treemaps (the circle-subdivision thread)

- Literature: Balzer & Deussen 2005; Nocaj & Brandes 2012 (power diagrams, area-targeted weight iteration — apvd-adjacent optimizer, converges without GD). d3 plugins: `d3-voronoi-map` / `d3-voronoi-treemap` (circle clipping built in, nestable).
- **Few-px cells are fine in a full-viewport VT** — same legibility economics as the existing treemap's few-px rects (the screenshot's tiny boxes). The earlier 30-40px caveat applies only to a *VT crammed inside a scatter marker* (a glyph): at 15px diameter, 20 cells ≈ sub-pixel slivers. Rule: glyph-VTs only above a diameter threshold or on hover; full-viewport VT unrestricted.
- Where VT actually beats rect-treemap: inside circular markers (§1), and aesthetics. Rect treemap keeps better label real estate. Not a replacement — a marker-glyph and maybe an alt view.

## 5. Filter plane (type-to-filter everything)

Text input (substring/regex) → every plot re-slices to matching paths ("just one project, even across buckets/dirs").

- The pruned tree.json can't answer substring filters at depth > its pruning (aggregates change when you exclude non-matching siblings). Real answer: **query the layer-2 parquet client-side** — duckdb-wasm (or parquet-wasm + HTTP range requests) over the published scan parquet; depth-sorted row groups already support depth-pushdown. This is the same machinery as the scan-browser spec — one investment, two features.
- v0 shortcut: filter at the *displayed* level only (match against visible node paths, re-normalize) — honest as "highlight + re-layout of current level", not a true re-aggregation; label it so.

### Indexes for fast substring/regex subtrees (design, 2026-08-15)

Key insight — **subtree semantics need no re-aggregation**: if a filter includes every subtree whose path matches, the answer set is the *maximal matching dirs* (matches with no matching ancestor), and layer-2 dir rows already carry full rollups. Serving = match dir paths + drop contained matches + read those rows. Per-file regex semantics (match files individually, re-aggregate) is the expensive rare case; defer.

Scale: dir rows ≈ 92.5M (CW) / ~2× fleet. Raw regex scan over that many strings ≈ seconds native, worse in wasm; substring defeats zonemaps/blooms. Index ladder (leverage/effort order):

1. **Segment dictionary + inverted index** (recommended): distinct path *components* are OoMs fewer than paths (heavy repetition — `trace_jobs`, run names, shard filenames). Regex runs over the segment dictionary (MBs — instant, in-browser OK) → matching segment ids → posting lists (segment id → dir-row ids, row-group-aligned for range-request fetch) → candidate dirs (verify full-path match for multi-segment patterns). Free byproduct: **autocomplete** — matching segments with byte totals shown before the filter commits.
2. **Trigram index at the segment level** (Code Search / Zoekt / pg_trgm design) if arbitrary within-segment substring needs to be index-fast: trigram → segment posting lists (10⁶ segments × ~10 trigrams — MBs), regex → required-trigram query per Cox; then segment→dirs as in 1. Path-level trigram postings would be GBs; two-level keeps it small.
3. **FM-index / suffix array** over concatenated dir paths — the asymptotically right substring index; heavy build, exotic chunked serving; only if 1+2 disappoint.

Artifacts 1-2 are small static files the site pipeline can emit next to tree.json — static CFP deploy keeps working; no server required for subtree semantics.

## Sequencing

1. Treemap age lens (§3) — hours, pure FE, uses `mtime_mean` (needs site pipeline to pass `--mean-mtime`; engines all support it as of DT 06af0d4).
2. Staleness scatter (§1) with sum-TBy iso-bands — new page/panel over layer-2 depth-slices; static data, no wasm needed at fixed depths.
3. Violin/histogram-per-child (§2) — needs per-child age histograms (query-time or small pipeline ext).
4. Filter plane (§5) — rides the scan-browser/duckdb-wasm track.
5. VT glyphs (§4) — last; needs d3-voronoi-map integration.
