# `@disk-tree/react`

disk-tree's **bytes-over-time** React widgets, built on the
[`@rdub/treemap`](../treemap) core — which this package **re-exports**, so
`import { Treemap, … } from '@disk-tree/react'` keeps working. New / non-disk
consumers should depend on `@rdub/treemap` directly; reach for this package when
you want the disk-flavored views below.

**Chart-lib-free** (DIY SVG), accessor-based.

| Widget | What |
|---|---|
| [`<TimeSeries>`](#timeseries) / [`<BytesOverTime>`](#timeseries) | Multi-series line/area chart with hover-follow crosshair. Zero deps. |

Everything from `@rdub/treemap` (`<Treemap>`, `useHoverPin`, `squarify`,
`DEFAULT_PALETTE`, `ageFade`, `parseQuery`, …) is also re-exported here. For the
treemap itself — including its `styles.css` and `/voronoi` subpath — see the
[`@rdub/treemap` README](../treemap/README.md).

## Install

Same SHA-pinnable **dist branch** mechanism as the core (via [`npm-dist`][npm-dist]):

```bash
pnpm add github:runsascoded/disk-tree#<dist-sha>
```

`react` and `react-dom` ≥ 18 are peer deps; `@rdub/treemap` comes along as a
dependency.

## `<TimeSeries>`

Multi-series line/area with hover-follow crosshair:

```tsx
import { TimeSeries } from '@disk-tree/react'

<TimeSeries<{ t: number; y: number }>
  series={[
    { key: 'a', label: 'foo', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }] },
    { key: 'b', label: 'bar', points: [{ t: 0, y: 5 },  { t: 1, y: 8 }] },
  ]}
  getX={p => p.t}
  getY={p => p.y}
  formatX={x => new Date(x).toLocaleDateString()}
  formatY={y => y.toLocaleString()}
  yScale="linear"       // or "log"
  area={true}
/>
```

And the convenience wrapper for the disk-tree default (bytes over time):

```tsx
import { BytesOverTime } from '@disk-tree/react'

<BytesOverTime
  points={[
    { time: '2026-08-01T00:00:00Z', bytes: 1000 },
    { time: '2026-08-05T00:00:00Z', bytes: 500 },
  ]}
  formatBytes={n => `${n.toLocaleString()} B`}
/>
```

## Theming

`.dt-timeseries` (grid, axis, tooltip, crosshair) themes the same CSS-var way as
the treemap; see the [`@rdub/treemap` theming section](../treemap/README.md#theming).

## Contributing

`packages/react/` is a workspace member of the [disk-tree] monorepo. Iterate from
the repo root:

```bash
pnpm install                                  # workspace-wide
cd packages/react
pnpm typecheck
pnpm test        # Vitest
```

The `site/` app consumes this package via `"@disk-tree/react":
"workspace:*"` — changes flow through instantly during `pnpm dev`.

## License

Apache 2.0 (same as disk-tree).

[disk-tree]: https://github.com/runsascoded/disk-tree
[npm-dist]: https://github.com/runsascoded/npm-dist
