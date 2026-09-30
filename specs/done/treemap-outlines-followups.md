# `@rdub/treemap` `outlineGroups`: follow-ups from mgu's first wiring

From **mgu** (`marin-gcs-usage`), after cherry-picking `48060e5` (`outlineGroups`, `specs/done/treemap-mark-union-outlines.md`) into its `packages/treemap` mirror and wiring keep/sweep marks to it. Three things needed fixing before it drew right there; all three are landed on mgu's copy (branch `gcs`, `packages/treemap/src/{outlines,OutlineOverlay,Treemap}.ts[x]`) and are asks to mirror upstream so the copies stay identical.

## 1. Float-epsilon seams draw as doubled lines (bug)

`unionOutline` buckets edge lines by the raw coordinate (`byX.get(r.x + r.w)` vs `byX.get(next.x)`). Squarify hands neighbors seams that agree only to float epsilon, so the two rects land on *different* lines, each bordered on one side only, and the shared seam strokes twice — inset 1 px each way, a 4 px bar where the feature is supposed to have removed the line. On a real map (mgu's `grug/`, ~1300 top-level cells) most interior seams of a same-key region showed as bars; the union perimeter was there but buried.

Fix: snap every coordinate to 1/8 px before bucketing (`q = v => Math.round(v * 8) / 8`, applied to `x`, `x + w`, `y`, `y + h` and the interval endpoints), and drop `hi <= lo` segments after `symDiff` (a zero-length segment with square caps still paints a `lw × lw` dot). Regression tests appended to `tests/outlines.test.ts` (`unionOutline: float-epsilon seams`).

## 2. `color()` can't return `var(--x)` (canvas paints black)

A canvas `strokeStyle` silently ignores a custom-property reference; mgu's fate colors are `var(--mk-keep)` etc. `OutlineOverlay` now resolves `var(--name[, fallback])` against the canvas element's computed style (`cssColor(el, color)`), passing any other string through. Worth doing in the core rather than every consumer, since `color` is documented as "a color the consumer supplies" and CSS variables are the normal way a themed app names one.

## 3. DOM renderer had no geometry for the overlay

`placedCells` was built only when `renderer === 'canvas'`, so with the default DOM renderer the overlay mounted with `cells=[]` and drew nothing. Now built when `renderer === 'canvas' || outlineGroups` (deps gain `!!outlineGroups`). The DT CIC didn't catch it because the scan treemap runs on canvas.

## 4. (Optional) `key()` doesn't see the collapsed chain

With `collapseChains`, `PlacedCell.path` ends at the chain's *deepest* node and `chainLabels` records the collapsed levels, but `key(node, path)` gets neither `chainLabels` nor a `chain` count — so a consumer that needs the cell's *own* node (to compare its mark with the parent cell's) has to replay the collapse from the path. mgu does that (`chainOf`: climb through lone-child parents); a third arg `{ chain }` (matching `renderCellExtra`'s `CellCtx.chain`) would make it unnecessary. Not blocking.

## Landing

Items 1–3 are one small commit's worth; mgu's copy is the reference (diff its `packages/treemap/src/` against `main`). When mirrored, mgu's next `/cp dt/main` pass will see them as patch-equivalent.

### Landed (dt)

Items 1–3 landed on `main`:

1. **Float-epsilon seams** — `unionOutline` now snaps every coordinate to ⅛ px (`q = v => Math.round(v * 8) / 8`, applied to the bucket keys `x`/`x+w`/`y`/`y+h` *and* the interval endpoints) before bucketing, and drops `hi <= lo` segments after `symDiff` (a zero-length seg with square caps would paint an `lw × lw` dot). Two regression tests appended to `tests/outlines.test.ts` (ε-offset seam still cancels; no zero-length seg at a snapped-collinear seam).
2. **`color()` = `var(--x)`** — `OutlineOverlay` resolves `var(--name[, fallback])` against the canvas element's computed style via a new `cssColor(el, color)`; any non-`var()` string passes through.
3. **DOM renderer geometry** — `placedCells` is built when `renderer === 'canvas' || outlineGroups` (memo deps gain `!!outlineGroups`), so the overlay has cells under the default DOM renderer too.

139 treemap tests pass; `tsc -b --noEmit` clean. Not CIC'd in dt: disk-tree's own UI doesn't wire `outlineGroups` (only mgu consumes it, and mgu CIC-verified its copy) — the fixes are logic + covered by the regression tests.

**Item 4 (`key()` chain arg) deferred** — non-blocking per the spec; mgu's `chainOf` path replay works. Left for a later pass if a second consumer needs the cell's own node.
