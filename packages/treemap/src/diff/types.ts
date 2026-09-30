import type { ReactNode, RefObject } from 'react'
import type { Tiling } from '../Treemap'

/** A row's diff verdict. `touched`: same size & count, mtime differs (rename /
 *  net-zero churn / `touch`). */
export type DiffStatus = 'added' | 'removed' | 'changed' | 'touched' | 'unchanged'

/** Node statuses add two synthetic kinds the tree builder introduces:
 *  `filler` (unenumerated unchanged bytes) and `fold` (aggregated dust). */
export type DiffNodeStatus = DiffStatus | 'filler' | 'fold'
export type DiffKind = 'file' | 'dir' | 'filler' | 'fold'

/** Area mode: `max` = one cell per row sized by `max(old, new)`, |Δ| painted as
 *  a band; `delta` = churn only, sized by `|Δ|`. */
export type DiffAreaMode = 'max' | 'delta'

/** A node in the diff treemap. Field names are metric-agnostic (`oldSize`/
 *  `newSize`/`countDelta`) so any part-of-whole diff maps onto it. */
export interface DiffNode {
  key: string
  label: string
  /** what the widget sizes by — `max(old, new)` or `|Δ|` per mode; parents take
   * `max(own, Σ children)` so children can never overflow. */
  weight: number
  /** signed primary-metric delta, for coloring */
  delta: number
  /** Σ of positive / negative deltas across descendants (frontier-leaf
   * granularity): `grew ≥ 0`, `shrank ≤ 0`, `grew + shrank ≈ delta`. */
  grew: number
  shrank: number
  /** same, for the secondary count metric (`countDelta`): `nGrew ≥ 0`, `nShrank ≤ 0` */
  nGrew: number
  nShrank: number
  status: DiffNodeStatus
  oldSize: number
  newSize: number
  countDelta: number
  kind: DiffKind
  uri: string
  /** fold cells: how many too-small-to-draw children were merged */
  nFolded?: number
  /** filler cells: the unenumerated unchanged children it stands for
   * (direct-child count and their descendants), when the server told us */
  nRest?: number
  nDescRest?: number
  /** Frontier dir with unexplored change below (budget/depth cut the walk). */
  pruned?: boolean
  children?: DiffNode[]
}

/** One recursive-frontier row (a change at some depth). */
export interface DiffRecRow {
  path: string
  uri: string
  depth: number
  kind: 'file' | 'dir'
  status: DiffStatus
  oldSize: number
  newSize: number
  delta: number
  countDelta: number
  /** Stats differ below but the walk stopped here (budget/depth). */
  pruned: boolean
}

/** One depth-1 flat row, for labeled grey context at the top level. */
export interface DiffFlatRow {
  path: string
  uri: string
  kind: 'file' | 'dir'
  status: DiffStatus
  oldSize: number
  newSize: number
}

/** Persisted full-diff index state for the pair. */
export interface DiffIndexInfo {
  status: 'none' | 'building' | 'done' | 'failed'
  error?: string
}

/** Everything the tree builder + treemap need about one compared subtree. The
 *  consumer maps its own compare payload onto this. */
export interface DiffInput {
  uri: string
  /** depth-1 rows (grey context; only `unchanged` ones are used) */
  flatRows: DiffFlatRow[]
  /** recursive change frontier across depths */
  recRows: DiffRecRow[]
  /** each expanded dir's biggest unchanged children (labeled grey cells) */
  unchangedTop: DiffRecRow[]
  /** aggregate of the rest, keyed by parent path (`''` = the compared uri) */
  unchangedRest: Record<string, { count: number; size: number; n_desc: number }>
  /** net primary-metric delta for the whole compared subtree */
  totalDelta: number
  oldRootSize: number
  newRootSize: number
  oldRootCount: number
  newRootCount: number
  index?: DiffIndexInfo
}

export interface DiffTreemapProps {
  input: DiffInput
  recState: 'loading' | 'error' | 'ready'
  onRecRetry: () => void
  onDrill: (uri: string) => void
  /** middle-click / open-in-new-tab href for a drillable dir */
  cellHref?: (uri: string) => string | undefined
  /** Fetch a node's own diff subtree (the widget rebuilds cells at its current
   *  area/unchanged mode). One request per drill, cached by the widget. */
  fetchSubtree: (node: DiffNode) => Promise<DiffSubtree>
  formatSize: (n: number | null | undefined) => string
  formatCount: (n: number | null | undefined) => string
  tiling: Tiling
  setTiling: (t: Tiling) => void
  /** The map box, measured for the drawable floor (see `mapMinFrac`). */
  mapRef: RefObject<HTMLDivElement | null>
  /** Chrome slots (plain fallbacks when omitted); a disk/MUI consumer supplies
   *  its own so the surface matches the rest of the app. */
  renderContainer?: (children: ReactNode) => ReactNode
  renderOverlay?: (recState: 'loading' | 'error' | 'ready', onRetry: () => void) => ReactNode
  renderEmpty?: (opts: { areaMode: DiffAreaMode; showUnchanged: boolean; onShowUnchanged: () => void }) => ReactNode
}

/** The raw sub-diff a drill fetches (no root aggregation). */
export interface DiffSubtree {
  recRows: DiffRecRow[]
  unchangedTop: DiffRecRow[]
  unchangedRest: Record<string, { count: number; size: number; n_desc: number }>
}

/** One metric column-group (old / new / Δ / bar) in the diff table. */
export interface DiffMetric {
  id: string
  /** group header ('Size', 'Descendants') */
  label: string
  /** only meaningful for directories (files render `-`) */
  dirOnly?: boolean
  fmt: (v: number | null) => string
  fmtDelta: (v: number) => string
}

/** One table row's per-metric old/new/Δ values. */
export interface DiffRowValues {
  [metricId: string]: { old: number | null; new: number | null; delta: number }
}

export interface DiffTableRow {
  key: string
  path: string
  uri: string
  kind: 'file' | 'dir'
  status: DiffStatus
  values: DiffRowValues
}

export interface DiffTableProps {
  rows: DiffTableRow[]
  /** the compared dir itself (the highlighted `.` summary row) */
  parent: { uri: string; values: DiffRowValues }
  metrics: DiffMetric[]
  /** count of unchanged entries omitted (for the footer note) */
  unchangedCount: number
  pageSize?: number
  /** leading icon for a row (app: folder/file glyph) */
  renderIcon?: (row: { kind: 'file' | 'dir' }) => ReactNode
  /** wrap a dir's path label in a link (app: SPA router Link) */
  renderPathLink?: (row: DiffTableRow, children: ReactNode) => ReactNode
  /** trailing per-row action cell (app: rescan button) */
  rowAction?: (row: { uri: string; kind: 'file' | 'dir'; path: string }) => ReactNode
}
