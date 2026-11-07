# Viz widgets: age lens, staleness scatter, age histograms, filter plane, Voronoi

Adopted upstream from marin-gcs-usage's `specs/viz-prototypes.md` (2026-08-15, the "crazy idea for a ~scatter plot" thread) — **disk-tree owns the reusable implementation** so every DT consumer benefits: FE widgets land in `@disk-tree/react` (accessor-based, chart-lib-free, like `Treemap`/`TimeSeries`), data-side enablers land in the Python pipeline/server. Downstream repos (mgu's site, DT's own `ui/`) keep only thin wiring: accessors, palettes, data fetch, and consumer-specific planes (e.g. mgu's GCS access-log atime join).

## 0. The score: sum-TB·years (additivity principle)

Each dir's TB·years score = **Σ over descendant files of `size_i × age_i`** — not `total_size × age(max mtime)` and not max-anything.

- **Monoid**: node score = Σ children's scores. Cascades like `size`; treemap-able as a size accessor with honest part-of-whole semantics.
- **Zero new columns**: Σ size·age = now·Σsize − Σ size·mtime = `size × (now − mtime_mean)`. The `mt_wsum` partial the engines already carry *is* Σ size·mtime, so sum-TBy is a query-time derivation per node.
- **atime mode**: same shape once per-path last-read exists (access-log agg: `max(ts)` per prefix). Consumer-supplied via accessor — the widgets never assume which age they're showing. (mgu caveats: unread files fall back to `max(logging_start, mtime)`; per-prefix grain; GCS only.)
- Deliverable: `sumTbYears(sizeBytes, mtimeMeanSec, nowSec)` + friends in a small `stats.ts` export (trivial, but names the contract).

## 1. Data-side enablers (Python)

The engines all support `--mean-mtime` (pandas/duckdb/stream, byte-identical; `mt_wsum` exact bigint). Gaps are in DT's own local path:

- **V.2a `disk-tree index -m/--mean-mtime`**: thread `mean_mtime` through the local gfind path (`find/index.py:aggregate` already accepts it; the CLI doesn't expose it). Per-file mtimes are already in the frame.
- **V.2b server passthrough**: `/api/scan` rows carry `mtime_mean` when the scan parquet has it (fresher-child patching: max-mtime stays the patched stat; `mtime_mean` comes from the parquet rows only — document that patch non-transitivity applies).
- **V.4b per-child age histograms**: byte-weighted mtime histograms per child of a drill dir — needed for §4 (means aren't distributions). Compute query-time over the layer-2 parquet at one drill level (depth-pushdown makes this cheap): `/api/scan?histogram=mtime&bins=24` returns per-child `[bin_edges, byte_totals]`. Same query shape works in duckdb-wasm for static-site consumers; a `disk-tree series`-adjacent CLI subcommand can emit them for pipelines.
- **V.5b path-segment index artifacts** (for the filter plane; design from mgu, adopted): distinct path *components* are OoMs fewer than paths. Emit (1) segment dictionary + (2) inverted index (segment id → dir-row ids, row-group-aligned for range-request fetch) next to a published scan parquet. Regex runs over the dictionary (MBs, in-browser OK) → posting lists → candidate dirs → verify full-path match. **Subtree semantics need no re-aggregation**: answer set = maximal matching dirs; layer-2 dir rows already carry full rollups. Free byproduct: autocomplete with byte totals. Escalation ladder if needed: segment-level trigram index (Cox/Zoekt design); FM-index only if that disappoints. Per-file-regex re-aggregation semantics: expensive, deferred.

## 2. Treemap age lens (V.1 — first: hours, pure FE)

Keep hue = category; modulate **lightness** by age: *older ⇒ more faded* toward panel bg ("fading from memory"). A composable lens, not a mode — stacks with any `colorForCell`.

- API: `ageFade(style, age01)` + `composeLens(base, lens)` helpers in `colors.ts`; consumers map their age accessor to `[0,1]` over a domain (helper: `ageDomain(nodes, getAge)`).
- Channel budget (from mgu, holds here): H = category, area = bytes, hatch = class lens, dim = highlight — L is the free channel. Caveats adopted: clamp the ramp (label contrast — e.g. fade floor ~45%); use **OKLCH lightness**, not RGB alpha, so equal age reads as equal fade across hues.
- Wire into DT `ui/` `ScanDetails` as the reference consumer (checkbox lens; v0 age = max-`mtime` which the API already serves — honest label "newest descendant"; upgrade to `mtime_mean` when V.2 lands).

## 3. Log-log staleness scatter (V.3 — "triage frontier")

New `<StalenessScatter>` component (DIY SVG, patterned on `TimeSeries`).

- x = age, y = bytes, log-log; one marker per dir at the drill level. Accessors: `getAge`, `getSize`, `getWeight` (marker radius ∝ n_files — the ops/listing-pain channel), `getHue` (link to treemap palette).
- **Iso-score diagonals are exact**: on this plot `y·x = size × mean-age = sum-TBy` (the §0 identity), so labeled diagonal bands (0.1 / 1 / 10 / 100 TB·yr) are true iso-score lines. Upper-right = delete-candidate frontier.
- Hover/pin via `useHoverPin`; click → consumer callback (drill / route).
- Voronoi-subdivided markers deferred to V.6; plain circles first.

## 4. Byte-weighted age histograms per child (V.4)

`<AgeHistograms>` (or violin variant): x = children of the drill dir, y = mtime; each child renders its **byte-weighted** distribution of descendant-file ages. Histogram is the honest v0 (violin = mirrored KDE later).

- **Area ∝ bytes** (not n_files, not age): each child's total area = its bytes, so the y-integral above an age threshold = reclaimable bytes at that threshold — expose a threshold slider callback that reports Σ bytes above it.
- "Voronoi the violin" resolved as **stacking**: subdivide each child's bars by *grandchild* (stacked segments, hues linked to the treemap palette) — area-∝ stays exact per segment, no geometry solver.
- Data contract: consumer supplies per-child `{ edges: number[], bytes: number[][] }` (optionally per-grandchild) — from V.4b server/CLI or their own query.

## 5. Filter plane (V.5 — type-to-filter everything)

Text input (substring/regex) → every widget re-slices to matching paths.

- **v0 (FE-only)**: filter at the displayed level — dim/re-layout visible nodes whose path doesn't match. Honest as "highlight + re-layout of current level", labeled as such (aggregates don't change when siblings are excluded). Implementable today via `colorForCell` dim + a `filterNodes` helper; a shared `<FilterInput>` + small context lets one input drive treemap + scatter + histograms.
- **v1 (true re-aggregation)**: query the layer-2 parquet client-side (duckdb-wasm / parquet-wasm + range requests; depth-sorted row groups already support pushdown) or server-side via `/api/scan` + a `filter=` param, using the V.5b segment index for candidate dirs. Autocomplete widget consumes the segment dictionary.

## 6. Voronoi treemaps (V.6 — last)

- Literature: Balzer & Deussen 2005; Nocaj & Brandes 2012 (power diagrams, area-targeted weight iteration). `d3-voronoi-map` / `d3-voronoi-treemap` have circle clipping built in.
- Verdict adopted from mgu: **not a rect-treemap replacement** — rects keep better label real estate. VT wins inside circular scatter markers (§3 glyphs) and as an aesthetic alt view. Glyph-VTs only above a diameter threshold or on hover (sub-pixel slivers below ~30–40px); full-viewport VT unrestricted (same legibility economics as few-px rects).
- Ship as a separate subpath export (`@disk-tree/react/voronoi`) so the core package stays dependency-free.

## Sequencing

1. **V.1** age-lens helpers (`colors.ts`) + `ScanDetails` checkbox lens (max-mtime v0). Pure FE. — **DONE**: `ageFade`/`ageDomain`/`age01` + a generic `lens` slot on `<Treemap>` (post-resolution style transform, stacks on any `colorForCell`); `ScanDetails` "Age lens" checkbox + tooltip age line; CIC-verified on the `/Users/ryan` scan (old repos fade, active dirs stay saturated, hue preserved).
2. **V.2** `disk-tree index -m` + `/api/scan` `mtime_mean` passthrough → lens upgrades to mean-age; `stats.ts` sum-TBy helpers. — **DONE**: `-m/--mean-mtime` on `disk-tree index` (`--measure-memory` moved to `-M`); local walk gives *every* inode `mt_wsum = size·mtime` (dir/symlink blocks cascade into `size`, so matching them keeps weights ≡ size — an empty dir keeps its own mtime instead of a nonsense epoch-0 mean); `load_or_create` rescans when a cached scan predates the column; server rescans (`/api/scan/start`) pass `-m` so UI-triggered scans feed the lens; `/api/scan` passes `mtime_mean` through (whole-row serialization) with NaN→null JSON sanitization; `stats.ts` ships `sumTbYears`/`formatTbYears`/`SEC_PER_YEAR`/`TB`; ScanDetails lens + tooltip prefer `mtime_mean` (label "mean mtime …") with max-mtime fallback.
3. **V.3** `<StalenessScatter>` + a DT `ui/` panel at the drill level. — **DONE**: DIY-SVG widget (log-log, marker area ∝ `getWeight`, hover/pin via `useHoverPin`, click-to-drill, unplottable nodes counted in a footer note rather than dropped); layout math split into a pure, separately-exported `scatter.ts` (`logDomain`/`logPos`/`logTicks`/`isoScoreSegment`/`decadesBetween`/`isoScoresForData`/`radiusFor`). Two design corrections from live data: iso decades are chosen from the **data's** score range, not the box corners (a corner is the combination of extremes, so corner-derived lines land in empty space), and `formatTbYears` steps the byte unit (`123 GB·yr`, not `1e-11 TB·yr`). `DEFAULT_PALETTE` consolidated into `colors.ts` so scatter markers and treemap cells share hues. DT `ui/`: `View: Treemap | Staleness` toggle, scatter plots the (filter-aware) direct children with `n_desc` as the count channel.
4. **V.4** histogram endpoint/CLI + `<AgeHistograms>` with threshold-slider reclaimable-bytes readout. — **DONE**: `src/disk_tree/histogram.py` (`age_histograms`, backend-agnostic — it takes a frame, so hybrid/duckdb/sqlite all work), `GET /api/histogram?uri&bins&limit&scan_id`, and `disk-tree histogram URI [-b bins] [-n limit] [-j] [-S]` with block-char sparklines. Endpoint deviates from the sketched `/api/scan?histogram=…`: it's a separate route because it needs *every* descendant file row (a distribution can't be rolled up from per-dir means, so there's no depth pushdown), and keeping it separate leaves the main view fast and lets the UI fetch it only when the view is open. `<AgeHistograms>` ships the threshold as a drag anywhere in the plot (no external slider), reporting `(epochSec, reclaimableBytes)`; `bytesOlderThan` splits the straddling bin linearly, matching what the bars draw. Both the CLI and the widget needed a **shape-only** mode (`-S` inverted / `normalize`): with children spanning orders of magnitude the honest shared scale renders everything but the biggest as a hairline. Omitted children are always reported, never silently dropped. Verified end-to-end: API bin sums match a direct parquet query exactly.
5. **V.5** filter plane v0 (`<FilterInput>` + level-filter helpers); segment-index artifacts + autocomplete as V.5b when a consumer needs cross-scan filtering. — **v0 DONE**: `filter.ts` (`parseQuery` — substring or `/regex/flags`, case-insensitive by default, invalid/half-typed regex degrades to substring rather than throwing on every keystroke; `filterNodes`; `dimUnmatched` for the treemap `lens` slot, so it *stacks* on the age lens instead of replacing it). No `<FilterInput>` component: DT already owns a filter box and the reusable part is the matching, not the input. One query now drives table + treemap (dim, placeholders included — they can't match) + scatter + histogram columns, labeled **"filtered (display only)"** because sizes still include hidden children. V.5b (segment dictionary + inverted index) stays deferred until a consumer needs cross-scan filtering.
6. **V.6** `@disk-tree/react/voronoi` glyphs/alt view. — **DONE**: `<VoronoiTreemap>` + `voronoiLayout` + geometry/PRNG helpers on the `@disk-tree/react/voronoi` subpath; `d3-voronoi-treemap`/`d3-hierarchy` are *optional peers*, so the core package stays dependency-free (pnpm defaults them to `dependencies` — they had to be moved). Three findings that sharpen the spec's "not a rect replacement" verdict, all now enforced in the API rather than left as folklore: (a) **wide value ranges are fatal** — the solver clamps tiny site weights, so a 0.1%-share child renders ~200% too big and a real 13-child listing measured **421,710%** worst-case area error; `minShare` (default 0.005) excludes them and the component always reports what it dropped (`25 too small to tessellate (13.8 MB)` on DT's own repo). Lowering `minWeightRatio` to 1e-6 does not rescue it (still 1506%). (b) The solver's `convergenceRatio` is a fraction of *clip* area, not per-cell relative error — at its 0.01 default a 20%-share cell lands ~5% off, so we default to 0.001 (~0.01% measured) and expose a separate `tolerance` for the `converged` flag. (c) It seeds from `Math.random`, so layouts reshuffle every render; a seeded mulberry32 (`seed` accepts a string, e.g. the dir URI) makes the picture a pure function of the data. DT `ui/` gets it as a fourth `View:` option, CIC-verified.

## Acceptance

- Widgets render in DT's own `ui/` (reference consumer, CIC-verified) and are consumable by mgu's site with only accessor wiring (their `/read dt` pickup).
- `@disk-tree/react` tests per widget (vitest, patterned on existing suites); Python tests for V.2/V.4b/V.5b artifacts.
- mgu retires the overlapping sections of their `viz-prototypes.md` in favor of this spec (their session, via `/read dt`), keeping only the atime/access-plane specifics.

## Status (2026-08-16): complete

V.1–V.6 all shipped and CIC-verified in DT's own `ui/` (`View:` toggle → Treemap + age lens / Staleness / Age histograms / Voronoi, all driven by one filter box). `@disk-tree/react` went 21 → 151 tests; Python 250 → 287.

Deferred by design, not forgotten:

- **V.5b** segment dictionary + inverted index. V.5 v0 (display-level filtering) covers the single-scan case; the index is what cross-scan / true-re-aggregation filtering needs, and it should be built when a consumer actually needs it rather than speculatively.
- **V.4's grandchild-stacked bars** ("Voronoi the violin" resolved as stacking). The endpoint returns per-child bins; per-grandchild would be a second `bytes[][]` dimension. Worth it when someone wants to see *which* subdirectory owns a child's old bulge.
- **V.3/V.6 crossover**: Voronoi glyphs *inside* scatter markers. Both pieces now exist (`voronoiLayout` takes any clip polygon, `circlePolygon` builds the marker's); wiring them is a small job whenever the density warrants it.

The atime plane stays consumer-specific (mgu's GCS access logs): every widget takes age through an accessor, so it needs no changes here.
