# Promote the diff treemap + table pair into `@rdub/treemap` core

The reference Flask UI has a full **diff view** — a treemap whose cells are
colored by change (green grew / red shrank / grey unchanged / hatched touched,
Δ encoded by a sub-rect area band) paired with a sortable **Δ table** (old / new
/ Δ per metric). It lives only in the app (`ui/src/components/CompareView.tsx`,
~1850 lines) and was never promoted. mgu wants a diff treemap and should get the
table with it; file-tree (and any other treemap consumer) should too.

Decision (2026-09-08): the pair lands in the **disk-agnostic `@rdub/treemap`
core** (not `@disk-tree/react`), so non-disk consumers get it. This follows the
`viz-widgets` precedent — *disk-tree owns the reusable impl; downstream repos
keep thin wiring* — but one package up, because a diff of *any* treemap-able
tree is not disk-specific.

## What's disk-specific today (must be lifted out)

`CompareView` is bound to the server's compare contract (`ui/src/api.ts`):

- `CompareRow` / `CompareResult` — flat child rows with `size`, `n_desc`,
  `size_delta`, `size_old`, `n_desc_delta`, `n_desc_old`, `status`.
- `CompareRecResult` — the recursive delta frontier (`rows` across depths,
  `expanded`/`pruned` flags, `unchanged: {top, rest}` grey context, `index`
  build status).
- Fetch coupling: `compareScans` / `compareScansRecursive` hit `/api/compare`;
  the treemap's drill `loadChildren` calls `compareScansRecursive` directly.
- App wiring in the table: `onScan` / `isScanning` / `getProgress` (rescan a
  path), `scan1` / `scan2` ids, breadcrumbs, routing.
- Two hard-coded metrics: **bytes** (`size`) and **count** (`n_desc`), each with
  its own formatter (`formatDelta`, `formatDeltaNumber`).

What is *not* disk-specific and should become the core contract: the status
taxonomy (`added | removed | changed | touched | unchanged`), the polarity/color
convention (see memory `diff-polarity-git-convention`), the Δ-by-area encoding,
the squarify/drill/hover machinery (already core), the table's sort/paginate/
sticky-header behavior, and the `unchanged: {top, rest}` grey-rollup idea.

## Proposed core API (`packages/treemap/src/diff/`)

Accessor-based, mirroring `Treemap`'s `CellCtx` style — the widget owns layout,
color, interaction; the consumer owns data shape and fetching.

```ts
export type DiffStatus = 'added' | 'removed' | 'changed' | 'touched' | 'unchanged'

/** One diff node the widgets render. Consumers map their own rows to this via
 *  accessors; the node itself is opaque `T`. */
export interface DiffAccessors<T> {
  key:      (n: T) => string          // stable id (path/uri)
  label:    (n: T) => string          // display name (basename)
  status:   (n: T) => DiffStatus
  /** Primary metric = treemap area. old/new; Δ derived. */
  oldSize:  (n: T) => number
  newSize:  (n: T) => number
  children?: (n: T) => T[] | undefined // already-loaded subtree, if any
  hasChildren?: (n: T) => boolean
}

/** A table metric column group (old / new / Δ). At least one; the first is the
 *  treemap's area metric. `fmt` formats a value; `fmtDelta` a signed delta. */
export interface DiffMetric<T> {
  id:    string
  label: string                       // 'size', 'items', …
  old:   (n: T) => number | null
  new:   (n: T) => number | null
  fmt:      (v: number) => string
  fmtDelta: (v: number) => string
}

export interface DiffTreemapProps<T> {
  root: T
  acc: DiffAccessors<T>
  areaMode?: 'max' | 'delta'          // cell area = max(old,new) | |Δ|
  showUnchanged?: boolean
  /** Drill fetch for a node whose children aren't loaded (one req/drill,
   *  widget-cached). Returns the node's diff children. Omit → no drill. */
  loadChildren?: (n: T) => Promise<T[]>
  onDrill?: (n: T) => void
  /** Optional grey-context rollup for a partially-expanded frontier. */
  unchanged?: DiffUnchanged<T>
}

export interface DiffTableProps<T> {
  rows: T[]
  acc: DiffAccessors<T>
  metrics: DiffMetric<T>[]            // ≥1; disk passes [bytes, items]
  onRowClick?: (n: T) => void
  /** Optional per-row action slot (disk: rescan button + progress). */
  rowAction?: (n: T) => React.ReactNode
  pageSize?: number                   // default 50
}
```

Colors/hatch/area-band + the swatch legend move into `diff/colors.ts` (reusing
`divergingColor` already in core). `DiffTreemap` composes the existing
`<Treemap>` with a `colorForCell` derived from `status` + Δ; it does **not**
fork the renderer.

## What stays in the app (thin wiring)

`ui/src/components/CompareView.tsx` shrinks to: fetch (`compareScans` /
`compareScansRecursive`), map `CompareRow[]`/`CompareRecRow[]` → the accessor
interface, supply `metrics = [bytes, items]`, wire `loadChildren` to
`compareScansRecursive`, and keep the app-only chrome (breadcrumbs, scan
pickers, routing, the rescan `rowAction`, the diff-index `building`→refetch
poll). No diff geometry, color, sort, or paginate logic remains in the app.

## Consumers

- **DT `ui/`** — the reference consumer; must stay byte-for-byte equivalent
  (same screenshots) after the refactor.
- **mgu** — wires its `/api/compare`-equivalent to `<DiffTreemap>` +
  `<DiffTable>`; gets the table it didn't have. (mgu consumes `@rdub/treemap`
  via a pinned `dist/treemap` branch — publish a new dist build after this
  lands; see memory `dist-branch-namespacing`.)
- **file-tree** — future; a diff of two file-trees is now expressible.

## Phasing

1. **Extract, no behavior change.** ✅ **Landed (2026-09-08).** `CompareTreemap`/
   `CompareTable` + helpers moved into `packages/treemap/src/diff/` (`colors.ts`,
   `types.ts`, `tree.ts`, `DiffTreemap.tsx`, `DiffTable.tsx`, `index.ts` — 1295
   lines); `CompareView.tsx` rewritten as a thin disk adapter (**1857 → 842
   lines**). Rather than a MUI/router/icons dependency in the core, the widgets
   take **injected render-slots** (`renderContainer`/`renderOverlay`/`renderEmpty`
   on the treemap; `renderIcon`/`renderPathLink`/`rowAction` on the table) — the
   app passes the same `<Paper>`/`<Link>`/`<FaFolder>` it used before, so the
   render is byte-identical. The sort caret (was a `react-icons` dep) is inlined
   as the exact FA `sort-up`/`sort-down` SVG paths. `DiffNode` uses disk-neutral
   field names (`oldSize`/`newSize`/`countDelta`); `DiffInput` is the flattened
   compare payload the app's `toDiffInput`/`toRecRow` map onto; `DiffMetric[]`
   drives the table columns (disk passes `[size, desc]`). Verified: `tsc -b`
   clean, vitest ui 25 / treemap 130 / react 91, `pnpm build` green, and CIC of
   the DT `/compare` view (all-unchanged + a real-churn pair) renders identically
   — green grew / red shrank / grey unchanged, band labels, legend, Δ table.
   **Follow-up:** no unit test for `buildDiffTree` yet — a golden test is a good
   phase-2 add.
2. **Export + document.** ✅ Exports added to `treemap/src/index.ts` (additive
   only, flow through `@disk-tree/react`). A short story/example still worth adding.
3. **Publish `dist/treemap`**, bump mgu's pin, wire mgu's diff view. *(open)*
4. (Later) file-tree adoption. *(open)*

## Risks / notes

- `@rdub/treemap` is pinned by external consumers — this **adds** exports, keeps
  the existing surface; no BIC.
- The recursive frontier (`unchanged: {top, rest}`, `expanded`/`pruned`) is the
  subtle part — keep its shape in the core `DiffUnchanged<T>` type rather than
  collapsing it, so the "biggest unchanged children + aggregate of the rest"
  grey context survives the move.
- Keep the polarity/area-band encoding exactly (memory
  `diff-polarity-git-convention`): green = added/grew, red = removed/shrank, Δ
  magnitude by area (sub-rect band), not saturation.
