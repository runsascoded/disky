/** On-the-fly scan-to-scan diff for the static compare Function — the
 *  serverless analog of the Flask `/api/compare` walk (spec
 *  `specs/public-diff-demo.md` Phase 3). Given two scans' rows *rebased to the
 *  compared uri* (path `.` = the uri, `child` = depth 1, `child/gc` = depth 2),
 *  it aligns them per path and emits the flat one-level child diff (the table)
 *  and a depth-≤2 frontier (the treemap): direct children, plus one level into
 *  each *changed* dir, with deeper-changed dirs marked `pruned` so the widget
 *  offers a drill (`DiffTreemap.fetchSubtree`). Undrawable rows (bytes and |Δ|
 *  both under the viewport byte-floor) are dropped. Statuses match
 *  `diff_index.py`: added | removed | changed | touched | unchanged. */
import type { TreeRow } from './parquet'

export type DiffStatus = 'added' | 'removed' | 'changed' | 'touched' | 'unchanged'

/** One path's status across the two scans (`diff_index.py` vectorized rules):
 *  present on one side only → added/removed; both with differing size / count /
 *  kind → changed; both equal but mtime moved → touched; else unchanged. */
export function diffStatus(a: TreeRow | undefined, b: TreeRow | undefined): DiffStatus {
  if (!a) return 'added'
  if (!b) return 'removed'
  if (a.size !== b.size || a.n_desc !== b.n_desc || a.kind !== b.kind) return 'changed'
  if (a.mtime !== b.mtime) return 'touched'
  return 'unchanged'
}

/** A row as `/api/compare` (non-recursive) returns it — the table shape. */
export interface CompareRow {
  path: string
  uri: string
  kind: 'file' | 'dir'
  parent: string | null
  status: DiffStatus
  size: number | null
  size_old?: number
  mtime: number | null
  n_desc: number | null
  n_children: number | null
  size_delta: number
  n_desc_old?: number
  n_desc_delta?: number
}

/** A row as `/api/compare?recursive=1` returns it — the treemap frontier. */
export interface CompareRecRow {
  path: string
  uri: string
  depth: number
  kind: 'file' | 'dir'
  status: DiffStatus
  size_a: number
  size_b: number
  size_delta: number
  n_desc_a: number
  n_desc_b: number
  n_desc_delta: number
  expanded: boolean
  pruned: boolean
}

export interface DiffSummary {
  added: number
  removed: number
  changed: number
  unchanged: number
  total_delta: number
}

/** A scan row rebased to the compared uri (`scan.ts` `toApi` output). */
export interface RebasedRow extends TreeRow { uri: string }

const size = (r: RebasedRow | undefined): number => r?.size ?? 0
const ndesc = (r: RebasedRow | undefined): number => r?.n_desc ?? 0

/** Index rebased rows by their uri-relative path. */
function byPath(rows: RebasedRow[]): Map<string, RebasedRow> {
  const m = new Map<string, RebasedRow>()
  for (const r of rows) m.set(r.path, r)
  return m
}

/** Direct children (relative depth 1) present in either side, by path. */
function childrenAt(
  parentRel: string,
  a: Map<string, RebasedRow>,
  b: Map<string, RebasedRow>,
): string[] {
  const want = parentRel === '.' ? 1 : parentRel.split('/').length + 1
  const under = (p: string) => (parentRel === '.' ? !p.includes('/') && p !== '.' : p.startsWith(parentRel + '/') && p.split('/').length === want)
  const out = new Set<string>()
  for (const m of [a, b]) for (const [p, r] of m) if (r.depth === want && under(p)) out.add(p)
  return [...out]
}

export interface DiffOptions {
  /** Absolute uri of the compared root (for `rowUri`). */
  uri: string
  /** `minFrac`: drop rows whose max bytes and |Δ| are both under
   *  `minFrac · max(rootA, rootB)` — undrawable cells. 0 keeps everything. */
  minFrac?: number
  /** Max frontier rows returned (best-first by |Δ|), default 2000. */
  maxRows?: number
}

const rowUri = (uri: string, rel: string): string =>
  rel === '.' ? uri : `${uri}/${rel}`

/** Absolute uri for a uri-relative child path, preferring a row's own `uri`. */
const childUri = (uri: string, rel: string, r?: RebasedRow): string =>
  r?.uri ?? rowUri(uri, rel)

/** Per-status counters (`touched` tracked internally but not in the response
 *  summary, which mirrors Flask's added/removed/changed/unchanged). */
type Counts = Record<DiffStatus, number>
const zeroCounts = (): Counts => ({ added: 0, removed: 0, changed: 0, touched: 0, unchanged: 0 })
const toSummary = (c: Counts, total_delta: number): DiffSummary =>
  ({ added: c.added, removed: c.removed, changed: c.changed, unchanged: c.unchanged, total_delta })

/** The flat one-level child diff (the table): every direct child of the uri. */
export function flatDiff(aRows: RebasedRow[], bRows: RebasedRow[], o: DiffOptions): { rows: CompareRow[]; summary: DiffSummary } {
  const a = byPath(aRows), b = byPath(bRows)
  const rows: CompareRow[] = []
  const counts = zeroCounts()
  let total = 0
  for (const rel of childrenAt('.', a, b)) {
    const ra = a.get(rel), rb = b.get(rel)
    const status = diffStatus(ra, rb)
    const delta = size(rb) - size(ra)
    counts[status] += 1
    total += delta
    if (status === 'unchanged') continue
    const r = (rb ?? ra)!
    rows.push({
      path: rel,
      uri: childUri(o.uri, rel, rb ?? ra),
      kind: r.kind,
      parent: '.',
      status,
      size: rb ? rb.size : null,
      size_old: ra ? size(ra) : undefined,
      mtime: r.mtime,
      n_desc: rb ? rb.n_desc : null,
      n_children: r.n_children,
      size_delta: delta,
      n_desc_old: ra ? ndesc(ra) : undefined,
      n_desc_delta: ndesc(rb) - ndesc(ra),
    })
  }
  rows.sort((x, y) => Math.abs(y.size_delta) - Math.abs(x.size_delta))
  return { rows, summary: toSummary(counts, total) }
}

/** The depth-≤2 frontier (the treemap) + labeled unchanged context. */
export function recursiveDiff(aRows: RebasedRow[], bRows: RebasedRow[], o: DiffOptions): {
  rows: CompareRecRow[]
  unchanged: { top: CompareRecRow[]; rest: Record<string, { count: number; size: number; n_desc: number }> }
  summary: DiffSummary & { expansions: number; truncated: boolean }
} {
  const a = byPath(aRows), b = byPath(bRows)
  const rootA = a.get('.'), rootB = b.get('.')
  const floor = (o.minFrac ?? 0) * Math.max(size(rootA), size(rootB))
  const drawable = (ra?: RebasedRow, rb?: RebasedRow): boolean => {
    const bytes = Math.max(size(ra), size(rb))
    return bytes >= floor || Math.abs(size(rb) - size(ra)) >= floor
  }

  const rows: CompareRecRow[] = []
  const unchangedTop: CompareRecRow[] = []
  const rest: Record<string, { count: number; size: number; n_desc: number }> = {}
  const counts = zeroCounts()
  let total = 0
  let expansions = 0
  let truncated = false

  const mkRow = (rel: string, depth: number, ra: RebasedRow | undefined, rb: RebasedRow | undefined, expanded: boolean, pruned: boolean): CompareRecRow => ({
    path: rel,
    uri: childUri(o.uri, rel, rb ?? ra),
    depth,
    kind: (rb ?? ra)!.kind,
    status: diffStatus(ra, rb),
    size_a: size(ra), size_b: size(rb), size_delta: size(rb) - size(ra),
    n_desc_a: ndesc(ra), n_desc_b: ndesc(rb), n_desc_delta: ndesc(rb) - ndesc(ra),
    expanded, pruned,
  })

  // Keep a dir's biggest unchanged children as labeled grey context, aggregate
  // the rest into one row (`key` `''` = the compared uri itself).
  const finalizeUnchanged = (key: string, tops: CompareRecRow[]) => {
    tops.sort((x, y) => Math.max(y.size_a, y.size_b) - Math.max(x.size_a, x.size_b))
    for (const t of tops.slice(0, 4)) unchangedTop.push(t)
    const agg = { count: 0, size: 0, n_desc: 0 }
    for (const t of tops.slice(4)) { agg.count += 1; agg.size += t.size_b; agg.n_desc += t.n_desc_b }
    if (agg.count) rest[key] = agg
  }

  // Depth 1: every changed direct child (best-first frontier). A changed dir is
  // expanded one level (depth 2). Unchanged children of the compared uri and of
  // each expanded dir become that dir's grey context.
  const rootTops: CompareRecRow[] = []
  for (const rel of childrenAt('.', a, b)) {
    const ra = a.get(rel), rb = b.get(rel)
    const status = diffStatus(ra, rb)
    if (status === 'unchanged') { counts.unchanged += 1; if (drawable(ra, rb)) rootTops.push(mkRow(rel, 1, ra, rb, false, false)); continue }
    counts[status] += 1; total += size(rb) - size(ra)
    if (!drawable(ra, rb)) continue
    const isDir = (rb ?? ra)!.kind === 'dir'
    // Descend only into a *changed* dir present on both sides (added/removed
    // subtrees are a single row by design).
    const expand = isDir && status === 'changed'
    rows.push(mkRow(rel, 1, ra, rb, expand, false))
    if (!expand) continue
    expansions += 1
    const tops: CompareRecRow[] = []
    for (const c of childrenAt(rel, a, b)) {
      const ca = a.get(c), cb = b.get(c)
      const cStatus = diffStatus(ca, cb)
      if (cStatus === 'unchanged') { if (drawable(ca, cb)) tops.push(mkRow(c, 2, ca, cb, false, false)); continue }
      if (!drawable(ca, cb)) continue
      // A changed dir at depth 2 isn't descended → mark pruned so the widget
      // offers a drill (`fetchable`: changed dir + non-zero Δ).
      const cIsDir = (cb ?? ca)!.kind === 'dir'
      rows.push(mkRow(c, 2, ca, cb, false, cIsDir && cStatus === 'changed'))
    }
    finalizeUnchanged(rel, tops)
  }
  finalizeUnchanged('', rootTops)

  rows.sort((x, y) => Math.abs(y.size_delta) - Math.abs(x.size_delta))
  const cap = o.maxRows ?? 2000
  if (rows.length > cap) { truncated = true; rows.length = cap }
  return { rows, unchanged: { top: unchangedTop, rest }, summary: { ...toSummary(counts, total), expansions, truncated } }
}
