# `(other)` tiles that say what they hide: a size-shaped hatch, and stats in the tooltip

Status: proposal, not started (2026-11-14). Nothing here is built. Prerequisite done: the hatch is visible on every render path (`10db997c6`, `packages/treemap/src/dust.ts`).

Ryan asked (2026-11-14) whether the hatch's grid lines could be spaced to approximate the hidden items' size distribution, and whether the `(other)` tooltip could carry summary stats or richer data. Short answers: yes to both, with one hard limit (the pixel budget, §2.3) and one data dependency (today's Worker never sees the hidden items, §3.1; VM mode does, §3.4).

## 1. What the hatch encodes today

`dustLines(w, h, count)` (`packages/treemap/src/dust.ts`) draws `dustLineCount(len, count, 3) = clamp(round(3·log2(count + 1)), 2, ⌊len / 4⌋)` rules per axis, at gaps growing geometrically (ratio 1.32) away from the lower-right corner. So:
- The only data in it is `count`, and only as ~log2(count) (8 folded items and 9 look the same; 1,000 and 10^6 differ by ~2x in rule count, until the 4 px floor caps both).
- The geometric gaps are decoration: they say "things get smaller toward the corner", not how much smaller, and they look the same for a tail of 1,000 equal 1 KB files and for one 9 GB file beside 999 tiny ones.

## 2. The math: a k × m grid as a size distribution

### 2.1 Cells as items

`k` vertical and `m` horizontal rules cut a `w × h` tile into `N = (k+1)(m+1)` cells; column `i` has width `a_i`, row `j` height `b_j` (`Σa = w`, `Σb = h`), and cell `(i, j)` has area `a_i·b_j`. Read each cell as an item, its area as the item's size:
- **Count:** `N = n` exactly when `n` factors as `(k+1)(m+1)` within the pixel budget; a prime `n` needs a 1-wide axis, and any `n` above the budget (§2.3) can't be matched at all.
- **Mean:** automatic. The areas sum to `w·h`, which the layout already made proportional to the tail's total bytes, so the mean cell area is `total / N` in the map's bytes-per-px² scale: right when `N = n`, and "each cell is `n/N` items" otherwise.
- **Spread:** always reachable in principle. Any `n` positive sizes have `CV ≤ √(n−1)` (equality: one item holds everything; `CV² = Σ(x/μ)²/n − 1 ≤ n − 1`). The grid reaches the same bound (one column and one row taking nearly all of `w` and `h`: `CV → √(N−1)`), and CV moves continuously from 0 (uniform grid) to that bound, so any target CV in `[0, √(N−1))` has a grid.

### 2.2 The product structure: log-sizes add

`log(a_i·b_j) = log a_i + log b_j`: over the `N` cells (each weighted once), the log-cell-area distribution is the convolution of the columns' log-width distribution with the rows' log-height distribution. So:
- `Var(log area) = Var(log a) + Var(log b)`, means add likewise.
- A **log-normal** tail is represented exactly: give each axis a log-normal with half the log-variance (`σ²/2` each); the sum of two independent normals is normal.
- An arbitrary shape is not, in general, a convolution of two discrete uniform-weight distributions. Two options, both O(lines):
  - **Split** (default): each axis takes the target's log-quantile function scaled by `1/√2` about its median. Mean and variance of log size match exactly; skew is shrunk by `1/√2` (a sum of two copies is more symmetric than one).
  - **One axis** (exact shape): columns carry the target's quantiles at full scale, rows are uniform. Log-cell-areas then have exactly the target's distribution, at half the dynamic range (§2.3).

### 2.3 The pixel budget caps count and range

Rules need a visible gap: today's floor is 4 px, and below ~3 px a 1 px rule reads as a fill, not a grid. Two consequences:
- **Count:** `N ≤ (⌊w/4⌋ + 1)(⌊h/4⌋ + 1)`. A 60 × 100 tile holds at most 16 × 26 = 416 cells; a 20 × 20 one, 36. Tails routinely hold 10^3–10^6 items. So for large tails the grid matches the distribution's **shape** (its quantiles), not its count, and each cell stands for `n/N` items; the count goes in the label (`(+N)` today) and the tooltip.
- **Range:** with every gap ≥ `g`, an axis of length `L` spans at most `log2((L − k·g)/g)` doublings of width, so cell areas span at most about `log2(w/g) + log2(h/g)` doublings: ~8.5 for 100 × 60 px at `g = 4` (a ~360x ratio). Hidden sizes in one folder can span 2^10–2^30 bytes. Beyond the range the grid saturates: map the target's log-quantiles into the available log-range with a monotone compression (linear while it fits, then scaled down so the extreme quantiles land on the floor), and say so in the tooltip (§4) rather than pretend the hatch is to scale. The one-axis option has half this range, so it's for narrow distributions (its range check decides, per tile).

### 2.4 Placement, per tile

Given a target log-size distribution (from moments or a histogram, §3) and the tile's `w × h`:
1. `N`: aim for `n` cells with the tile's aspect, `k + 1 ≈ √(n·w/h)`, `m + 1 ≈ √(n·h/w)`, each clamped to the budget (§2.3); never below today's 2 rules per axis.
2. Per axis, the `k+1` widths are `exp` of the axis distribution's quantiles at `(i − ½)/(k+1)`, normalized to sum to `w`.
3. If the narrowest is under `g`, shrink the axis's log-scale (bisection on the scale factor; the quantiles are fixed) until it isn't; record the factor (the compression, §2.3).
4. Sort widths descending left → right and heights descending top → bottom, so the smallest cells sit in the lower-right corner, as today (the texture keeps its "shrinking toward the corner" read, and the corner's density now means something).

Cost: O(k + m) per tile plus O(log) bisection steps; a quantile from a 40-bucket histogram is a lookup and a linear interpolation. No change to the layout (the tile's area and position are unchanged); only the rules move.

With no distribution data (today's server `(other)`, a filtered view, §3.1), the renderer keeps today's count-only geometric spacing: the two looks must be distinguishable, or a viewer reads decoration as data. Proposal: the data-driven hatch drops the dashed frame's 2-2 dash for a solid hairline frame, and the tooltip says which it is.

## 3. Where the distribution comes from

The renderer needs, per `(other)`, one of two summaries of its hidden items' sizes (each hidden item = one child of the folder not drawn, sized by its subtree total, as the map sizes it):
- **Moments (4 numbers):** `n`, `Σs`, `Σ log2 s`, `Σ (log2 s)²` (zero-size items counted in `n` and kept out of the logs, with their count beside). Gives the count, mean, geometric mean and log-σ: enough for the log-normal placement (§2.2). Sums merge by addition, so they compose across shards (VM mode's merge, §3.4) and subtract (§3.3).
- **Histogram (~40 numbers):** counts per `⌊log2 s⌋` bucket (sparse: `[[bucket, count], …]`, usually a dozen nonzero). Gives the shape (median, quantiles, multi-modal tails) for the split or one-axis placement, and the tooltip's sparkline. Also merges and subtracts by addition.

Both are per side in a diff; the diff map sizes cells by `max(old, new)` or `|Δ|` (`AreaMode`), so the summary is over that metric, computed where both sides are known.

### 3.1 Today's Worker can't see the hidden items

The Worker builds `(other)` by subtraction (`site/functions/_lib/view.ts`: `rest = subtract(a, kidAggs)`, `folded` from the row's `n_children`); the read plan prunes row groups whose `b_max` is under the threshold precisely so it never touches the sub-threshold children. So eager per-`(other)` stats would cost the reads the cell rule exists to avoid. Two ways around it without VM mode, §3.2 and §3.3.

### 3.2 Lazily, on hover or pin (today's Worker)

A route (`/api/other?path=P&scan=S&skip=…`, or `/api/subtree` with a flag) reads P's children from the `path` sort (depth `dP + 1`, path range `[P/, P0)`: one contiguous range, O(#children) rows), drops the drawn ones, and returns `n`, the moments, the histogram and the top ~10 by size. Fired when the tooltip opens on an `(other)` tile (debounced), cached at the edge like any view (and a `RESPONSE_V` bump on the new field, per the usual rule). Bounded: a folder with more than a cap of children (e.g. 2^18) returns a sampled histogram with `approx: true`, or nothing past the cap; huge flat folders are where the stats matter most, so this is the main weakness. Unfiltered and filtered views alike (the read applies the view's filters per row), at the cost of a second request per hovered tile. The hatch itself stays count-only until the stats arrive, then redraws.

### 3.3 Precomputed per folder (index columns)

At index build (`path-index`, `tiers`, the interval store's fold), each folder row gains its children's histogram (and moments). The hidden set's summary is then exact and free: the folder's histogram minus its drawn children's buckets (their sizes are in the response). The Worker returns it eagerly with every `(other)`. Costs and limits:
- Storage: a sparse histogram per folder row; parquet's dictionary + run-length encoding should keep it small, but it needs measuring on gcs before committing (phase 0 sizing, as with the path store).
- Versioning: a child's size change changes its parent's histogram, but it already changes the parent's size, so the interval store versions the same rows it does today; no new churn.
- Unfiltered plain views only: an owner, class or name filter changes which children count and how big they are, and the column can't be re-derived under it. Filtered views fall back to §3.2 or count-only.
- Diffs: the subtraction works per side; the `|Δ|` area mode's distribution isn't a difference of two histograms, so the diff map uses `max` side summaries or falls back.

### 3.4 VM mode: exact, eager, under every filter

`vmd` ([vm-mode.md] on `r/rw/vm-mode`, `bench/full-scan` there) visits every node of the view in its pass and applies the cell rule in the merge (§2.2–§3 of that spec: `(other)` holds "the exact rest … with `folded` = its nonempty children not drawn"). Accumulating, for each `(other)`, the four moments, the 40-bucket histogram and a bounded top-k heap (k = 10) is O(1) extra work per hidden node, in the same pass, exact under scan pins, diffs (per side, and `|Δ|` directly), owner / class / staged scopes and search. It also merges across shards by addition (the moments, the histograms) and by heap merge (the top-k). This is the clean path: the stats arrive with the view, no second request, no cap. The answer shape gains the fields on `(other)` nodes, and `vmAdapter.ts` maps them to the same `ViewNode` fields as §3.2/§3.3, so the renderer doesn't care which produced them.

### 3.5 What needs what

| Piece | Today's Worker | Index columns (§3.3) | VM mode (§3.4) |
| --- | --- | --- | --- |
| Distribution-shaped hatch on client folds (`(+N)` from `mergeSmall`, the core's `FoldedNode`) | yes: the client holds the folded items' sizes | — | — |
| Hatch on server `(other)`, plain views | lazy (§3.2), after hover | eager | eager |
| Hatch on server `(other)`, filtered views / diffs | lazy, capped | no (count-only) | eager |
| Tooltip: count, total, mean | yes (today's `folded`, bytes, objects) | yes | yes |
| Tooltip: median, quantiles, sparkline | lazy | yes | yes |
| Tooltip: top ~10 items (name + size) | lazy | no (names aren't in a histogram) | yes |

## 4. The tooltip

For an `(other)` tile, in this order:
- **Count and total:** "4,212 items · 37 GB" (today's `folded` and bytes; objects beside).
- **Typical size:** median and mean ("median 2.1 MB, mean 8.8 MB"); a mean far above the median says "a few big ones", in words a viewer reads at a glance.
- **Sparkline:** the log2 histogram as ~40 thin bars, the x-axis labeled at its extremes (`1 KB … 4 GB`), the median marked.
- **Largest items:** the top ~10 by size, name + size, each a link that drills to it (they're real paths), with "+4,202 more" under them.
- **What the hatch shows:** one line, "Hatch spacing follows these sizes (compressed 3x to fit)" or "Hatch density shows the count only", so the texture is never mistaken for more than it is.

Stats that haven't arrived (§3.2) show a skeleton for the lines that need them; count and total are always there.

## 5. Mapping the cursor to a real item

With `N = n` and items assigned to cells by rank, a cell would *be* an item, and hovering it could name it, as the canvas's `dustChildAt` already does for client folds whose items it holds. For server `(other)` tiles this is drawing below the threshold: naming every cell needs every hidden item, which is exactly what the threshold avoids sending. Only the largest few are worth it: the top-k (§3.4, or §3.2's lazy read) can claim the top-left (largest) cells, each hit-testable and labeled in the tooltip, with the rest of the grid anonymous. Not proposed for phase 1.

## 6. Phases

1. **Renderer, client folds only.** `CellStyle.dustDist` (`{ n, moments } | { n, hist }`), the placement of §2.4 in `dust.ts` (`dustRules` gains a distribution mode beside today's geometric one), all three renderers (canvas, DOM, SVG export). Fed first by folds whose items the client holds: the core's `FoldedNode` and the site's `mergeSmall` `(+N)` tiles. No server change. Unit tests on the placement (exact rule positions for fixed inputs, the CV and mean checks of §2.1, the saturation of §2.3).
2. **Tooltip + lazy stats (today's Worker).** The route of §3.2 and the tooltip of §4, for server `(other)` tiles, plain and filtered. Needs a cap and its measurement on gcs's largest flat folders.
3. **Index columns (optional).** §3.3, only if phase 2's lazy request proves too slow or too frequent; gated on a sizing pass on gcs.
4. **VM mode.** §3.4's accumulation in `vmd`'s merge and its adapter fields, landing with VM mode's site wiring; phases 2–3's routes stay for deployments not on a VM.

## 7. Open questions for Ryan

1. Is a texture whose spacing is data (but compressed past ~8 doublings, §2.3) better than today's honest-but-decorative one, given the tooltip carries the real numbers? The alternative is to keep the hatch count-only and put all the distribution in the tooltip.
2. Phase 1's split vs one-axis default (§2.2): the split doubles the range, the one-axis is exact in shape.
3. Phase 2's cap on lazy reads (children per request), and whether a capped folder shows sampled stats or none.
4. Whether to skip §3.3 entirely and wait for VM mode for eager stats.

[vm-mode.md]: https://github.com/runsascoded/disky/blob/rw/vm-mode/specs/vm-mode.md
