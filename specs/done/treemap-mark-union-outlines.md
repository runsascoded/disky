# Treemap: outline the *union* of adjacent same-group cells, not each cell

Asked for by **mgu** (`marin-gcs-usage`, the mark-&-sweep consumer of `@rdub/treemap`). Its keep/sweep marks render as a colored frame per cell. When several *sibling* cells each carry the same mark and sit side by side, every one draws its own frame and the region reads as a lattice of doubled lines (a grid of kept run dirs → a chain-link fence), instead of one bordered area.

mgu already suppresses redundant frames at the app level: a cell only draws an edge where its fate differs from its parent's (a kept parent renders once; same-fate descendants drop out). That rule can't help here — these are siblings, each with its own mark, so each legitimately owns a frame. The fix needs neighbor geometry, which only the core has.

## Ask

A **grouped-outline overlay**: the consumer names a group per cell; the core strokes, once, the outer perimeter of the union of all rendered cells sharing a group — on a canvas over the map area, in a color the consumer supplies.

```ts
outlineGroups?: {
  /** Group key for a cell, or null for none. Same key ⇒ same region. */
  key: (node: T, path: T[]) => string | null
  /** Stroke for a group. */
  color: (key: string) => string
  width?: number            // CSS px, default 2
  /** Stroke inside the union (default) or centered on its boundary. */
  inset?: boolean
}
```

- **Geometry**: cells are axis-aligned rects on one plane, so the union boundary is the set of rect edges *not shared* with a rect of the same group — edge-cancellation over the group's rects (sort edges by axis/position, drop pairs that coincide with opposite orientation), then stroke what remains. No polygon library needed. A group's rects need not be contiguous; disjoint islands just get their own loops.
- **Nesting**: a group can contain both a parent cell and its children only when they render as separate cells (e.g. a title-barred branch). Treat the parent's rect as the region and skip children whose key matches — the union already covers them.
- **Folds / dust**: a `(+n)` fold or dust tile takes its own key from `key()` like any cell; if the consumer returns the same key as its neighbors it joins the region.
- **Layering**: the overlay sits above cell fills and the seam borders, below tooltips/badges, `pointer-events: none`. Redraw with layout (resize, drill, tiling change).
- **Per-cell frames stay the consumer's**: this doesn't replace `renderCellExtra`; mgu would stop drawing per-cell mark frames for grouped cells and let the overlay carry them.

## How mgu would use it

`key` = the mark's identity (`prefix + ts`) for a cell whose effective mark is its *own* (not inherited), `color` = the fate's color (keep green / sweep red / last-ckpt amber). Adjacent siblings marked separately but with the same fate could optionally key on the fate alone to merge into one region; that's a consumer choice, which is why `key` is a function.

## Non-goals

- Non-rectilinear outlines, rounded corners.
- Changing default rendering; with no `outlineGroups` nothing changes.

## Landed (DT, 2026-09-09)

Shipped as specified. `outlineGroups?: OutlineGroups<T>` on `<Treemap>`:

- **Geometry** (`packages/treemap/src/outlines.ts`, pure + unit-tested):
  `groupRects` buckets placed cells by `key`, keeping only the *outermost* cell
  per key on any path (a same-key descendant is already covered by its ancestor —
  the nesting rule), so a group's rects never overlap. `unionOutline` derives the
  boundary by symmetric-difference of the occupancy intervals at each edge line
  (a vertical line borders the group where a rect ends but none begins, and vice
  versa), returning segments with inward normals — handles partial edge overlaps
  and disjoint islands, no polygon library. `tests/outlines.test.ts`: exact
  segment lists for single / adjacent-merge / disjoint / L-shape(partial-overlap)
  rects + the nesting and sibling grouping.
- **Overlay** (`OutlineOverlay.tsx`): a `<canvas>` over the map area,
  `pointer-events:none`, above fills, below tooltips; strokes each group's
  segments, offset inward by `width/2` by default (square caps close corners),
  redrawn on layout change (placed cells / dims). DPR-scaled.
- Renderer-agnostic (reads the shared placed cells); exports added to
  `@rdub/treemap` (`OutlineGroups`/`OutlineSeg`/`OutlinePath` types,
  `groupOutlines`/`groupRects`/`unionOutline`, `OutlineOverlay`); additive, no
  BIC. Verified: package `tsc` clean, 137 tests pass, and CIC of a DT scan
  treemap keyed by top-level subtree — each subtree drew one inset perimeter,
  adjacent same-group siblings merged (`dist`'s two children under one border),
  nested children didn't double-outline.

mgu's side (wiring keep/sweep marks to `key`/`color`) is theirs to add.
