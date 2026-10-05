# Treemap shared-edge tiling + depth-scaled borders (core)

Ryan (8/27): rects should be able to **share edges** instead of each carrying
its own borders with gutters between. Today each cell insets 2px per side, so
painted area under-represents by ~perimeter/area — worst exactly where it
matters, dense leaf fields (a 6×6px cell paints 4×4 = 44% loss; 200×200 loses
2%). With gutters, area-proportionality cannot hold at the highest and lowest
displayed levels simultaneously; shared edges make it near-exact (cells laid
at exact float coords; browser AA handles fractional px — the gutters were the
systematic bias, not rounding).

## Rendering

- **Shared mode**: no insets — each cell occupies its exact squarify rect.
  Boundaries drawn once per edge via the border-collapse trick (each cell draws
  top+left; the container closes bottom+right), or inset hairline box-shadows.
  The stroke itself is the only residual error; attribute half to each
  neighbor and keep it ≤1px at leaves.
- **Depth-scaled borders**: thicker strokes at shallow levels for hierarchy
  legibility, tapering to 1px (or 0.5px hairline on hidpi) at the deepest
  visible level.
- Honest residual: branch **title bars** still consume child-canvas, so
  sibling-relative areas are exact but cross-parent comparisons skew slightly.
  Follow-up option: overlay labels (no reserved bar) as another mode.

## API (all user-selectable, defaults = today's look)

```ts
tiling?: 'gaps' | 'shared'
  | ((n, path, depth, ctx: CellCtx) => 'gaps' | 'shared')
  // per-subtree: e.g. auto-'shared' for dense leaf fields
  // (median child cell area < ~100px²), comfy gaps elsewhere
borderWidth?: (depth: number, ctx: CellCtx) => number
  // default gaps-mode: current 2px insets; shared-mode default: max(1, 3 − depth)
```

- `'gaps'` stays default (back-compat). The density-adaptive callback is the
  expected marin setting: gaps at container levels, shared for leaf swarms.
- Interacts with `CellStyle.segments` (makeup stripes): in shared mode the
  stripe inset shrinks to the stroke width.

## Notes

- Hover/click targets unchanged (cells still own their rects; borders paint on
  top). `dust` cells (<14px min-dim) probably always shared.
- CP target: this is a core (`packages/react`) feature — track in
  `specs/done/dt-core-upstreaming.md`.
