# `<Treemap onCellHover>` — outward hover, to finish file-tree's scrub

`@rdub/file-tree`'s split view (listing above treemap, one shared
`TreeSource`) now cross-highlights **table → map**: hovering a listing
row emphasizes the matching tile. It does this purely with props you
already expose — a `lens` keyed to an externally-controlled
`highlightedPath` rings the matched cell. Live-verified against the pin
`0c486606` (`0.1.0-dist.e758294`).

The **map → table** direction — hover a tile, light its row — is the
half we can't build, because `<Treemap>` has no outward hover signal.
This spec asks for the one small prop that unblocks it.

## The ask

Add an optional callback to `TreemapProps<T>`:

```ts
/** Fired when the pointer enters a cell (with the node + its path from
 *  root) and again with `null` when it leaves the map / all cells. Lets a
 *  consumer mirror the map's hover into another view — e.g. file-tree's
 *  split listing highlights the row for the hovered tile. Debounce/grace
 *  is the consumer's concern; fire on the real enter/leave. */
onCellHover?: (n: T | null, path: T[]) => void
```

You already track exactly this internally: `pin.hover(cellKey)` /
`pin.hover(null)` at the cell mouse handlers (and the map's
`onMouseLeave`) in `Treemap.tsx`. The ask is just to also call an
optional `onCellHover(node, path)` from those same spots — `null` node
on leave. No behavior change when the prop is absent.

`path: T[]` (the node array root→cell, same shape `onPathChange` /
`getId` use) is what the consumer needs: file-tree maps it back to a
listing row via `getId`/the node's own path, exactly the inverse of the
`highlightedPath` match it already does.

## Why a callback, not a controlled prop

The other direction needed no new API — `lens` + a re-render already let
an *input* highlight flow in. Hover is an *output*, and there's no
prop-shaped way to read it: `renderTooltip` fires during render (can't
`setState` from it), and `onCellClick` is a click, not a hover. So a
callback fired from the existing hover handlers is the minimal fit.

## Consumer side (already staged, will light up when this lands)

- `TreemapRendererProps` (file-tree's pluggable-map contract) will grow
  `onHoverPath?: (path: string | null) => void`; the reference
  `<TreeMapView>` will forward `onCellHover` → `onHoverPath` (mapping the
  hovered node to its tree-relative path).
- `DirView` already owns the shared `hovered` state and passes
  `highlightedPath` to both panes; it will also pass `onHoverPath` to the
  map, so a tile-hover sets `hovered`, which lights the listing row (the
  row-highlight-in path is already built and working).

So on your side it's ~3 lines (call `onCellHover` where `pin.hover` fires);
on ours it's the forward + one `DirView` wire. Bidirectional scrub then
works with zero consumer glue — free in `<FileTree>` split view.

## Not in scope

- No grace-timer / debounce in the callback — fire raw enter/leave; the
  consumer decides. (file-tree's listing hover is per-row, not per-pixel,
  so it's already cheap.)
- No pinned/selected state — this is hover only. Selection, if ever
  wanted, is a separate `onCellClick`-driven concern.

## Implemented (2026-09-01)

Landed on `<Treemap>` exactly as specced — `onCellHover?: (n: T | null, path:
T[]) => void`, fired from the shared hover handler (`activateHover`, the one
choke point both the DOM and the new canvas renderer route through) and
`null`-ed alongside every `pin.hover(null)` (map leave, tooltip leave, canvas
leave). Refinements over the raw ask:

- **Once per cell change, not per pixel.** A `hoverKeyRef` gates the callback so
  a mousemove *within* the same cell doesn't re-fire; it fires on the enter of a
  new cell and once on leave. (Still no grace/debounce of our own — the `null`
  rides the existing 180ms leave grace, i.e. it fires where `pin.hover(null)`
  already fired.)
- **Both renderers.** Because it hangs off `activateHover`, a canvas tile hover
  fires it too — no extra wiring when a consumer flips `renderer="canvas"`.

Unit-tested (exact enter→`['bar',['root','bar']]`, dedup, leave→`[null,[]]`;
plus an inert-when-absent case). Ships in `@disk-tree/react`; file-tree gets it
on its next dist re-pin.
