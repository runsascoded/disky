# Every treemap is the treemap: the diff map as the site widget, docked tips, per-cell loading

**Why.** Reviewing the first two-bucket day (2026-09-17) showed the Diff map lagging the main map in every way the main map has been polished: it bypasses the site's `Treemap` wrapper (no tiling/renderer prefs, its own legend and bar), it dims the whole map behind an "aligning" overlay long after depth 1 has rendered, a bucket the older scan never covered shows as one flat tile, and single-child chains render as a cascade of title bars. Separately, hovering up and down a lineage bounces the floating tip around the screen on both maps.

## 1. The diff map is the site `Treemap`

`DiffTreemap` becomes a *mode* of `site/src/Treemap.tsx` (`diff={data}`): the same wrapper, the same ⚙ (tiling, renderer) and ⛶ on the footer row, the same crumb bar (movement arithmetic as its suffix, the page's drill via `onPathChange`), the same tip renderer with the diff's numbers in it, the same canvas/DOM choice. What stays diff-specific is the colouring (`Δ / size`, `first scanned` blue), the area mode (`max` / `Δ`) and the legend swatches — a `colorForCell`/`renderLegend` pair the wrapper already takes.

- **Chains collapse** as on the main map: the `(unchanged)` filler must not count as a second child. Add fillers *after* chain detection, or mark them `chainInert`, so `tmp/ttl=14d/skyrl/users/benfeuer` reads as one title, not five.
- **One-sided subtrees expand** (shipped with this spec in `_lib/view.ts`): a node present on one side still expands to its children (all `added`/`removed`), so a first-scanned bucket shows its shape.

## 2. Per-cell loading, not a map-wide veil (all maps)

The wrapper's `Busy` overlay dims the whole map while any fetch is in flight. Progressive loading already paints depth 1 first; the veil should follow the data:

- A cell whose subtree is still loading carries a `pending` flag (the wrapper knows which subtree queries are in flight: `subtreeQs[i].isPending` ↔ the drilled node's children; for the diff, the "aligning" phase is per expanded row).
- `@rdub/treemap` gains `pendingCell?: (node, path) => boolean` and draws a quiet in-cell marker (a small spinner at the title bar's end in DOM, a pulsing hatch in canvas), never an overlay. The map-wide veil remains only for the first paint (no tree at all).
- The diff's "aligning the rows…" becomes the pending state of the rows being aligned; the subtitle's loading text goes.

## 3. Docked tips: `tipMode`

`@rdub/treemap` `tipMode?: 'float' | 'dock'` (default `float`). In `dock` mode the hover tip renders into a fixed slot the consumer provides (`renderTipSlot`, or a default panel under the map's footer row), so moving through a lineage updates one panel in place instead of a tip chasing the pointer. Pinning still works (the slot stays); a phone always docks (there is no hover, a tap pins into the slot). The site sets `dock` for both maps and puts the slot under the footer row, above the table — the row that already holds the totals.

## 4. Tests

- `_lib/view.test.ts`: a one-sided node with children expands (rows for each child, all `added`).
- `@rdub/treemap`: chain collapse ignores `chainInert` children; `pendingCell` marks render (DOM snapshot of the marker's presence); `tipMode: 'dock'` renders the tip into the slot and never positions it.
- CIC: the diff at the root after a bucket's first scan shows its top-level dirs; hovering `marin → datakit → cluster/…` keeps one panel; the diff's rows being aligned show in-cell markers while the depth-1 cells stay bright.
