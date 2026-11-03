# Treemap scaling: from DOM cells toward a canvas-backed map

**Moved to `specs/done/` 2026-10-22** (gcs's spec sweep, Ryan's go): Done: the canvas renderer and its a11y overlay shipped in `packages/treemap/src/Treemap.tsx`. Color-buffer hit testing stays an optional proposal.

Status: planning. Captures the roadmap for scaling `<Treemap>` past the DOM-per-cell ceiling, and records what already shipped so the canvas work is a plan, not a rewrite-in-the-dark.

## The problem

Each `<Treemap>` cell is an absolutely-positioned `<div>` (~3–4 DOM nodes: the cell, its `.dt-treemap-bg` paint layer, label, inner container). There's no cap and no virtualization. This is comfortable at hundreds of cells and janky at thousands — an mgu map is firmly in the 1e3–1e4 realm, and drilling/resizing re-lays and re-paints the whole field. The layout math (`squarify`) is cheap; the cost is DOM nodes, style recalc, and paint.

Two forces make this worse than a flat node count suggests:
- **Nested tiles**: a container cell renders its children inline, so a shallow drill can mount a whole subtree of cells at once.
- **Long tails**: real filesystems have a power-law size distribution — a handful of big dirs and a very long tail of tiny ones. Drawing the tail as individual cells is the bulk of the node count *and* the least legible part of the map (sub-hoverable slivers).

## What shipped (2026-08-31)

These are the incremental wins that both improve legibility now and de-risk the canvas move:

- **Adaptive edge-contrast** (`edgeContrast`, default on): each shared-tiling cell derives its own half-stroke from its composited face luminance (dark stroke on light cells, light on dark), so grey-on-grey fields keep readable borders. `contrastEdge`/`parseColor` live in `@disk-tree/react`'s `colors.ts`. — commit `7735b91`
- **Textured "(other)" dust tile** (`dustTexture`, default on): the built-in fold tile renders as a `<canvas>` cross-hatch (`<DustHatch>`) whose rules tighten toward the lower-right and whose density scales with the folded count, on a faded ground in a dashed frame — visibly distinct from real cells. **This is the first hybrid div-plus-canvas cell, and the first canvas hit-test**: hovering the tile maps the cursor to the specific folded child via a squarify of the folded items over the cell box (guarded to ≤4000 items), so the tail is interrogable with *one* canvas instead of N divs. — commit `3f35a98`
- **Live fold control** (`foldControl`, default off): a "detail" slider in the chrome bar scales both fold thresholds by one multiplier (`minCellArea` linearly, `minCellSide` by √), so a viewer trades legibility against completeness without a code change. — commit `bc96f8e`

The dust tile is the template for everything below: a region that would be thousands of cells, drawn as one canvas, with position→item hit-testing computed from the layout rather than from DOM elements.

## Roadmap

### 1. Generalized hybrid: canvas for dense fields, divs for the drillable spine (near-term)

Today only the *folded* tail is a canvas. Extend the same treatment to any dense leaf field: when a node's laid-out children are mostly below a legibility threshold (e.g. `medianChildArea < ~120px²`, the cue the `tiling` callback already computes), draw the whole field to one canvas layer instead of mounting a cell per child. Keep real `<div>` cells for:
- the big, drillable cells (they need hover chrome, drill affordance, labels, links),
- the current drill spine and its immediate children.

Hit-testing for the canvas field reuses the dust approach: keep the field's squarify rects in a closed-over array, map pointer → rect → node on `mousemove`. Tooltips and click-to-drill route exactly as they do for the dust tile.

Payoff: the node count drops from "every cell" to "the big cells + one canvas per dense field" — the 90% case for the tail without touching the interaction model.

Open question: labels. A canvas field can draw its few largest members' labels itself (measure text, skip when it won't fit), which we already do implicitly by leaving the big cells as divs. Decide whether the canvas ever draws text or stays purely areal.

### 2. Progressive rendering — **shipped (canvas), 2026-09-01**

`TreemapCanvas` paints biggest-cell-first, budgeted: the first frame paints
synchronously up to an 8 ms budget (small/medium maps finish there, and it's
correct in a backgrounded tab where rAF is paused), and any remainder streams
over subsequent animation frames at ~10 ms each, so a 1e4–1e5-cell map never
blocks the main thread. Biggest-first is free and exact: a container's rect
contains its descendants', so area-descending *is* an ancestor-before-descendant
order (children paint over their parent) — so the first frame is the whole map's
skeleton, refined by later frames, in one flat sorted pass with no special
casing. The canvas isn't cleared between chunks (they accumulate); a
layout/size/style change re-runs the paint from a clean clear. Hit-testing works
off the retained tree immediately, before paint finishes.

**Finding — layout is the next floor, not paint.** With paint bounded, the
`flushSync` first-frame window at 40k un-folded cells dropped 1072 → ~700 ms,
and the residual is now `layoutCells`/`squarify` (a single squarify of 40k
sibling rects), not paint. Two things keep this from mattering yet: (a) real
maps fold the tail (`minCellArea`), so the laid+drawn count is bounded by
viewport/threshold, not the raw node count — you never squarify 40k *visible*
cells; (b) it's a flat-tree artifact (no nesting to defer). A faster or
incremental layout (lay the top level + big cells first, defer deep subtree
layout — which dovetails with the existing lazy `loadChildren` drill model) is a
separate lever if a real folded map ever shows layout cost. `ResizeObserver`
re-lays already repaint progressively through this same path.

### 3. Fully canvas-backed treemap (medium-term) — **in progress; approach pivoted**

One `<canvas>` (or a small fixed set of layers) draws the entire map; DOM holds only the chrome bar, tooltip, and overlays. This is the "see how performant it can get" target — saving 1e4–1e6 DOM nodes.

**Pivot (2026-09-01): go here directly, skip the §1 hybrid.** The hybrid (canvas fields injected into a DOM tree) means two rendering models interleaved and hit-testing split between DOM events and canvas rewalks — fiddly to reason about and to test. A *standalone canvas renderer, swapped in as a whole*, is one code path, one paint loop, one hit-test — the "battle-tested renderer you reuse." So §1 is superseded by a clean renderer switch, and §2 (progressive) folds into the canvas paint pass.

**Architecture — a `renderer` prop, not a second component.** All of `<Treemap>`'s complexity is renderer-agnostic — drill state (controlled/uncontrolled), lazy-load (`fetched`/`pending`/`failed`), fold/layout, the tooltip/pin machinery, keyboard, resize, and the chrome bar. Only the recursive `cell()` (paint + hit-test) is renderer-specific, and there the layout math is *entangled* with the div emission. So:

- **`renderer?: 'dom' | 'canvas'`** (default `'dom'` — zero change for every current consumer). It swaps only the map body; everything around it is written once.
- **Extract `layoutCells()`** (`layout.ts`, pure): the tree → a placed-cell tree (`PlacedCell<T>`: absolute `x/y/w/h`, `depth`, `mode`, `boxW/boxH`, `edge`, `dust`, `showLbl`, `children`) — geometry + node identity only, no style, no paint. Written to match `cell()`'s formulas exactly so the DOM path can later adopt it too (single source of truth for layout). `FoldedNode`/`isFolded` move here.
- **Lift the hit→action closures** (`showTip`/`onClick`/`dustHitAt`) to operate on `(node, path)` so a canvas hit and a DOM event call the same handler.
- **`<TreemapCanvas>`** paints the placed tree to one canvas (fill+fade, `contrastEdge` stroke, dust via a shared `drawDust(ctx,…)` factored out of `DustHatch`, labels above a size threshold) and hit-tests the retained placed tree (deepest-hit-wins), reporting `(node, path)` up.

Feature parity is staged: first cut covers fill/edge/dust/labels/drill/tooltip; segments (makeup stripes), chain-collapse, `cellHref` anchors, and the lazy-load overlay follow. `cellHref`/Vimium/crawlability is the one thing canvas can't do natively — keep a thin off-screen anchor list mirroring the visible drillable cells (the a11y contract below).

Design decisions to settle in this spec before building:

- **Hit-testing via an offscreen color-id buffer.** Render each cell to a hidden buffer in a unique color keyed to its index; on pointer move, read the 1px under the cursor, decode → cell index → node. O(1) per event, independent of cell count, and exact (no squarify re-walk). Rebuild the id-buffer only on layout change. Alternative considered: a spatial index (grid/R-tree) over the rects — simpler to reason about, but O(log n) and needs its own structure; the color-buffer is the standard trick and pairs naturally with the paint pass.
- **Retained vs immediate mode.** Keep the laid-out rect list (retained) and repaint from it; drills/resizes recompute the list and repaint. Avoid per-frame re-squarify.
- **Progressive + lazy tooltips.** In canvas mode the tooltip's data can be fetched lazily on hover (the hit-test yields a node id; the tooltip body — subtitle, breakdown — loads on demand, client- or server-side). This removes per-cell tooltip cost entirely.
- **Text.** Draw labels only for cells above a size threshold; batch by font to minimize state changes. Consider a DOM overlay for just the handful of always-labeled big cells (best text rendering) atop the canvas field.
- **Accessibility / links — shipped (2026-09-01).** A single `<canvas>` has no native focus, `cellHref` anchors, Vimium hints, or crawlability. Restored via a **thin DOM overlay**: transparent, `pointer-events:none` `<a>`/`<button>` elements positioned over the largest cells (`a11yLinks`, default on). The canvas still handles every mouse interaction (the overlay is invisible to the mouse); the overlay exists for keyboard focus, screen readers (`aria-label` = label + size), Vimium, real `cellHref` links, and crawlers. Focus scrubs (`onCellHover` + the tooltip) and shows a `:focus-visible` ring on the cell; Enter/activate routes through the same drill/pin path as a mouse click. **Bounded** so the node count stays a fraction of the DOM renderer's: `a11yMaxCells` (default 400, largest cells first) and `a11yMinSide` (default 0) are the "how many / how big" knobs. The DOM renderer ignores these (its cells are already real elements). CIC-verified: focusable, Enter-drills, focus ring + tooltip, mouse passes through, count bounded. This closes the last canvas parity gap.
- **The dust texture and edge-contrast port directly**: both are already pixel math (`DustHatch` draws to canvas; `contrastEdge` returns a stroke color), so they carry over unchanged — a point in favor of this direction.

**Interaction refinements (2026-09-01, from mgu feedback).** (a) **`edgeEmphasis`** (both renderers, default 0): multiplies the shared stroke width by `1 + e·max(0, 2−depth)`, so top-level boundaries read at a glance without hand-writing a `borderWidth` ramp — mgu's "hard to see top-level edges." (b) **Canvas cursor** now goes `pointer` over drillable/`cellHref` cells (was always `default`), matching the DOM renderer. (c) **Pinned cell is ringed** on the canvas (+ the pinned tooltip gets an accent border) — a click-to-pin was previously invisible, so the map looked frozen when hover got suppressed. (d) **Tooltip anchors to the cell's top-left** (both renderers) instead of the pointer's entry point, so placement no longer depends on which edge you moused in from.

### 4. Fold-threshold config near the TM — done

Shipped as `foldControl` (§What shipped). The canvas modes should keep honoring the same multiplier, since folding still decides how much of the tail becomes a dust/hatch region vs. individual marks.

## Benchmark: canvas vs DOM (2026-09-01)

A2A harness (`tmp/playground/bench.{html,tsx}`, `window.__benchSweep`): a fresh
`<Treemap>` of N cells mounted into a fixed 1400×900 box, folding off (drawn
cells = N), `flushSync` so the full React reconcile + DOM construction + (for
canvas) the whole `useLayoutEffect` draw are measured synchronously — plus a
forced `offsetHeight` reflow so the DOM renderer pays its style+layout cost.
**Browser rasterization is excluded for both**, which only the DOM renderer
pays on top, so every number is *conservative toward DOM*. Median of 2.

Flat (root → N leaves):

| cells  | DOM ms | DOM nodes | canvas ms | canvas nodes |
|-------:|-------:|----------:|----------:|-------------:|
|  1,000 |     67 |     2,666 |        11 |            1 |
|  5,000 |    192 |    10,000 |        54 |            1 |
| 15,000 |  1,039 |    30,000 |       188 |            1 |
| 40,000 |  4,922 |    80,000 |       999 |            1 |

Nested (root → √N dirs → √N leaves each — the representative shape, with title
bars + inner containers):

| cells  | DOM ms | DOM nodes | canvas ms | canvas nodes |
|-------:|-------:|----------:|----------:|-------------:|
|  1,000 |    340 |     2,814 |        55 |            1 |
|  4,000 |    839 |     8,316 |       146 |            1 |
| 12,000 |  2,289 |    24,639 |       543 |            1 |

**Verdict: the rewrite is justified.** Canvas is 4–6× faster to construct and
holds at exactly **one** DOM node vs the DOM renderer's 2–5× cell count (up to
80k) — the node blowup is what drives style recalc, memory, and GC on every
resize/drill. Nested containers cost the DOM renderer ~5× more per cell (340ms
for 1,000 nested vs 67ms flat); canvas is indifferent to nesting. The DOM
renderer's super-linear curve makes a 12k-cell real map a ~2.3s freeze (paint
excluded); canvas is ~0.5s. Canvas was not yet *instant* at the top end (~1s at
40k) — §2 progressive rendering (now shipped) bounds the first frame to its 8 ms
paint budget and streams the rest, leaving layout (`layoutCells`) as the residual
first-frame cost (see §2's finding). Color-id-buffer hit-testing (§3) stays
deferred: pointer-move on the retained tree hasn't shown up as a cost.

## Sequencing

**Superseded by the 2026-09-01 pivot.** The original plan sequenced §1 (hybrid) → §3 (rewrite). We go to §3 directly via the `renderer` prop, since a whole-map canvas swap is *simpler* than the hybrid, not riskier: the DOM path is untouched (default), so the canvas path can land incrementally behind the flag and be measured against the DOM baseline before it's ever a default. The color-id-buffer hit-test is a later optimization — the first cut hit-tests the retained placed-cell tree (deepest-hit-wins, O(depth) per event, exact), which is enough to prove the renderer; swap in the color buffer only if pointer-move cost shows up in the mgu-scale profile.

## Consumers

All of this lands in `@disk-tree/react`, so disk-tree's `ui/`, mgu, and file-tree get it by version bump (git-SHA re-pin for external consumers). mgu is the stress case (largest maps) and should be the benchmark target for §3.
