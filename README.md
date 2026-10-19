# `@open-athena/treemap`

A SOTA React **treemap** — and the layout/color primitives behind it.
**Chart-lib-free** (no Plotly, no recharts; d3 only for the optional Voronoi
variant) — everything is DIY SVG, plain DOM, or a single `<canvas>`. All
components are accessor-based, so any node shape works.

Ships:

| Export | What |
|---|---|
| [`<Treemap>`](#treemap) | Squarified layout with **two renderers** (SVG/DOM default + whole-map `<canvas>`), drill-on-click, pin-on-leaf tooltip, keyboard nav, fold-small "…" dust tiles, shared-edge (gapless) tiling, per-cell coloring + [emphasis `ring`](#brushing), fullscreen, slot-based coloring/tooltip/legend/rollup, lazy subtrees. |
| [`<VoronoiTreemap>`](#voronoitreemap) | Circle- (or polygon-) clipped Voronoi tessellation, areas ∝ value. Separate `@open-athena/treemap/voronoi` subpath — the only part with dependencies. |
| [`useHoverPin`](#usehoverpin) | Headless hover+pin state (single pin, touch-safe, outside-click / Esc clear). |
| `<TitleSpaceToggle>` | Controlled checkbox for `reclaimTitleSpace`; share its `checked` value with `<Treemap reclaimTitleSpace={checked}>`. |
| `squarify` / `foldSmall` / `foldThin` / `squarifyRemainder` | Pure layout primitives if you want to render the cells yourself. |
| `layoutCells` / `flattenPlaced` / `hitTest` / `isFolded` | Placed-cell layout + hit-testing, shared by both renderers. |
| `resolveCellStyle` / `foldedOf` | Cell-style resolution (the `colorForCell` → `lens` → contrast-edge fall-through). |
| `DEFAULT_PALETTE` / `parseColor` / `contrastEdge` / `CONTAINER_BG` | Color primitives (CSS-var-friendly). |
| `divergingColor` / `divergingInk` | Red/green diverging scale for Δ views. |
| `ageFade` / `ageDomain` / `age01` | Age lens: fade cells toward the panel background by any secondary scalar (`color-mix` in OKLCH, so equal age reads as equal fade across hues). Compose via the treemap's `lens` slot — stacks on any `colorForCell`. |
| `parseQuery` / `filterNodes` / `dimUnmatched` | Filter plane: substring or `/regex/` path matching (a half-typed regex degrades to substring, never throws), plus a dim transform for the treemap's `lens` slot. |

> disk-tree's **bytes/mtime/age-domain** widgets (`<TimeSeries>` / `<BytesOverTime>`,
> `<StalenessScatter>`, `<AgeHistograms>`, `sumTbYears`) build on this core and
> live in [`@disk-tree/react`](../react), which re-exports everything here.

## Install

`reclaimTitleSpace` defaults to `false`: parents reserve a full-width title bar. Enabling it lets complete columns to the right of the measured label reclaim that strip, while all sibling rectangles retain one shared area multiplier. It works with DOM, canvas, hit-testing and export. `branchOutline="…"` adds quiet, one-pixel parent frames above descendants without changing layout; those frames also appear in exported images.

```bash
pnpm add @open-athena/treemap
```

```ts
import '@open-athena/treemap/styles.css'
```

The npm package is compiled (ESM + types per subpath: `@open-athena/treemap`, `/squarify`, `/parseColor`, `/divergingColor`,
`/svg`, `/voronoi`), so it needs no JSX/TSX handling in your bundler. It's also on a SHA-pinnable **dist branch** (via
[`npm-dist`][npm-dist]), the same build:

```bash
pnpm add github:runsascoded/disk-tree#<dist-sha>
```

Or, with [`pnpm-dep-source`][pds]:

```bash
pds init packages/treemap
pds gh treemap   # switch to the latest GH dist SHA
```

You'll also need to `import '@open-athena/treemap/styles.css'` once somewhere in your
app for the default cell/tooltip styling.

`react` and `react-dom` ≥ 18 are peer deps.

## `<Treemap>`

```tsx
import { Treemap } from '@open-athena/treemap'
import '@open-athena/treemap/styles.css'

interface Node { name: string; size: number; children?: Node[] }

const root: Node = {
  name: 'root', size: 300,
  children: [
    { name: 'foo', size: 200, children: [
      { name: 'a.txt', size: 100 },
      { name: 'b.txt', size: 100 },
    ] },
    { name: 'bar', size: 100 },
  ],
}

<Treemap<Node>
  root={root}
  getSize={n => n.size}
  getChildren={n => n.children}
  getLabel={n => n.name}
  // Optional slots — every one has a working default.
  formatSize={n => `${n} B`}
  renderer="dom"          // or "canvas" (one <canvas> for the whole map)
  tiling="gaps"           // or "shared" (gapless — neighbors abut, half-stroke edges)
  colorForCell={(n, path, depth) =>
    // Return `null` to defer to the default 8-slot categorical palette.
    n.isPlaceholder ? { bg: '#4a4a52', ink: '#d0d0d8' } : null
  }
  renderTooltip={(n, path) => <>{n.name}<br/>{n.size} B</>}
  renderLegend={(current, path) => …}
  renderRollup={(current, path) => …}
  onCellClick={(n, path, e) => { /* return true to skip built-in drill/pin */ }}
  onCellHover={(n, path) => { /* outward hover signal for external brushing */ }}
  onPathChange={path => console.log(path.map(n => n.name).join('/'))}
/>
```

Built-in interaction: click a branch to drill in, click a leaf to pin its
tooltip, Backspace/Escape pops the drill stack, Escape/outside-click unpins.

### Brushing / selection: `CellStyle.ring`

A `colorForCell`/`lens` result can set `ring` to give a cell an emphasis border —
honored in **both** tiling modes (unlike `edge`, the shared-mode gutter
half-stroke), painted as a box-shadow (DOM) or a stroked rounded-rect (canvas)
so it never affects layout:

```tsx
lens={(n, path, depth, ctx, style) =>
  n.path === selectedPath ? { ...style, ring: { color: '#78aaff', width: 3 } }
  : n.path === hoveredPath ? { ...style, ring: '#fff' }   // shorthand: color at default width
  : style
}
```

`ring` is `string | { color: string; width?: number; inset?: boolean }` (default
width 2px, `inset` true). It follows the cell's corner radius and composites over
whatever structural `edge`/gutter the cell already has.

### Lazy subtrees

A tree that arrives one page at a time — disk-tree's
`/api/scan?uri=…&depth=N`, a pruned static `tree.json` — can't say "this node has
children" through `getChildren`, because nothing in hand and genuinely a leaf look
identical. Two slots close that gap:

```tsx
<Treemap<Node>
  root={root}
  getSize={n => n.size}
  getChildren={n => n.children}          // what this page carried
  hasChildren={n => n.n_children > 0}    // what the server says exists
  loadChildren={async n => fetchChildren(n.uri)}
  renderLoading={n => `Loading ${n.name}…`}
  renderLoadError={(n, path, err, retry) => <button onClick={retry}>{err.message} — retry</button>}
  onChildrenLoaded={(n, path, kids) => cache(n, kids)}
/>
```

A node is drillable when it has children *or* can fetch them. Only the **viewed**
node loads — one request per drill, not one per cell on screen — and results are
cached per node for as long as `root` is unchanged, so drilling back in is free.
Rejections render with a retry; without `loadChildren`, `hasChildren` is ignored
entirely, so eagerly materialized consumers are unaffected.

## `<VoronoiTreemap>`

An organic alternative to rectangles, from a **separate subpath** so the core
package stays dependency-free:

```bash
pnpm add d3-voronoi-treemap d3-hierarchy   # optional peers
```

```tsx
import { VoronoiTreemap } from '@open-athena/treemap/voronoi'

<VoronoiTreemap<Row>
  items={children}
  getValue={r => r.size ?? 0}
  getLabel={r => r.path}
  shape="circle"          // or "rect", or pass an explicit `clip` polygon
  seed={uri}              // same dir ⇒ same layout, every render
  formatValue={formatSize}
  onCellClick={r => navigate(pathFor(r))}
/>
```

Read the caveats before reaching for it — they're why `<Treemap>` stays the
default:

- **It cannot render a wide value range.** The solver clamps tiny site weights,
  so a child holding 0.1% of the bytes comes out ~200% too large and drags the
  whole tessellation off target (measured: 421,710% worst-case error on a
  13-child listing). `minShare` (default 0.005) drops those, and the component
  reports how many and how much. Rect treemaps have no such floor.
- **It is iterative, not exact.** `converged` / `error` report the worst per-cell
  *relative* area error; the component labels the chart when it misses `tolerance`
  (default 2%). Note the solver's own `convergenceRatio` is a fraction of the
  *clip* area, a different and much weaker guarantee — hence our tighter 0.001
  default.
- **It is randomly seeded.** Unseeded, the same data lays out differently on every
  render; `seed` (number or string) makes it a pure function.
- Labels need room, so cells under ~900px² get none.

`voronoiLayout` is exported for custom rendering, along with `circlePolygon`,
`rectPolygon`, `polygonArea`, `polygonCentroid`, `maxAreaError`, `toPointsAttr`,
and the `mulberry32` / `hashSeed` PRNG.

## `useHoverPin`

Headless single-pin state — pairs with any hover UI:

```tsx
import { useHoverPin } from '@open-athena/treemap'

function Legend({ items }: { items: string[] }) {
  const pin = useHoverPin<string>({ excludeRefs: [] })
  return (
    <>
      {items.map(k => (
        <span
          key={k}
          onMouseEnter={() => pin.hover(k)}
          onMouseLeave={() => pin.hover(null)}
          onClick={() => pin.togglePin(k)}
          style={{ opacity: pin.active === k ? 1 : 0.4 }}
        >
          {k}
        </span>
      ))}
    </>
  )
}
```

## Theming

Every color and background is a CSS custom property with a sensible default —
override on any ancestor:

```css
.dt-treemap {
  --dt-treemap-container-bg: #202024;
  --dt-treemap-ink: #d0d0d8;
  --dt-treemap-folded: #4a4a52;
  --dt-treemap-tip-bg: #1a1a1e;
  /* … */
}
```

## Contributing

`packages/treemap/` is a workspace member of the [disk-tree] monorepo (where the
widget is developed against a real consumer). Iterate from the repo root:

```bash
pnpm install                                  # workspace-wide
cd packages/treemap
pnpm typecheck
pnpm test        # Vitest
pnpm test:watch
```

The DT app under `ui/` and `@disk-tree/react` both consume this package via
`"@open-athena/treemap": "workspace:*"` — changes flow through instantly during
`pnpm dev`.

## License

Apache 2.0 (same as disk-tree).

[disk-tree]: https://github.com/runsascoded/disk-tree
[npm-dist]: https://github.com/runsascoded/npm-dist
[pds]: https://github.com/runsascoded/pnpm-dep-source
