import type { AgeRow, TreeNode } from './types'
import { compareScans, federatedEvents, type FederatedView, type SourceCatalog } from './federatedScans'

export interface SourceTree { source: string; digest: string; tree: TreeNode }

/** Reader adapter: mount bounded source responses without exposing timing in
 * TreeNode or changing the treemap renderer. Missing reads stay partial. */
export function composeFederatedTrees(view: FederatedView, results: readonly SourceTree[]): { tree: TreeNode; missing: string[] } {
  const reads = new Map(results.map(r => [r.source, r]))
  if (reads.size !== results.length) throw new Error('duplicate source tree response')
  const c: TreeNode[] = [], missing: string[] = []
  for (const s of view.sources) {
    const r = reads.get(s.source)
    if (s.state !== 'ready' || !s.scan || !r) { missing.push(s.source); continue }
    if (r.digest !== s.scan.digest || r.tree.b !== s.scan.total_bytes || r.tree.o !== s.scan.total_objects) throw new Error('source tree does not match the pinned scan')
    c.push({ ...r.tree, n: s.mount })
  }
  c.sort((a, b) => b.b - a.b || a.n.localeCompare(b.n))
  return { tree: { n: 'All projects', k: 'dir', b: c.reduce((v, n) => v + n.b, 0), o: c.reduce((v, n) => v + n.o, 0), c }, missing }
}

/** Existing age renderer consumes absolute epoch-day bins unchanged. Prefix
 * only the grouping identity; never shift object timestamps to scan time. */
export function mountFederatedAgeRows(mount: string, rows: readonly AgeRow[]): AgeRow[] {
  return rows.map(r => ({ ...r, d1: r.d1 ? `${mount}/${r.d1}` : mount }))
}

export interface FederatedSeriesPoint { at: string; bytes: number | null; objects: number | null }
export interface FederatedSeries { source: string; points: FederatedSeriesPoint[] }

/** One source's observations: values at canonical UTC instants, in selection order (`compareScans`; a later
 * entry at the same instant supersedes an earlier one). */
export interface Trace<T> { source: string; points: readonly { at: string; value: T }[] }
export interface Step<T> { at: string; values: (T | null)[] }

/** The step function behind every federated over-time view: one step per union event (each distinct instant any
 * source observed at, ascending), each source's value there its latest observation at or before it — carried
 * forward between its own observations, null (unknown) before its first. `values` follow `traces`' order. */
export function carryForward<T>(traces: readonly Trace<T>[]): Step<T>[] {
  const events = [...new Set(traces.flatMap(t => t.points.map(p => p.at)))].sort((a, b) => a < b ? -1 : a > b ? 1 : 0)
  const sorted = traces.map(t => [...t.points].map((p, i) => ({ ...p, i })).sort((a, b) => a.at < b.at ? -1 : a.at > b.at ? 1 : a.i - b.i))
  const cursors = traces.map(() => -1)
  return events.map(at => ({
    at,
    values: sorted.map((pts, k) => {
      while (cursors[k] + 1 < pts.length && pts[cursors[k] + 1].at <= at) cursors[k]++
      return cursors[k] < 0 ? null : pts[cursors[k]].value
    }),
  }))
}

/** Source traces at union event times. Null before the first observation, and
 * carry forward afterward. Consumers draw steps, not interpolated growth. */
export function federatedSeries(catalogs: readonly SourceCatalog[]): FederatedSeries[] {
  const ordered = [...catalogs].sort((a, b) => a.source < b.source ? -1 : a.source > b.source ? 1 : 0)
  federatedEvents(catalogs) // its checks: no duplicate source or mount, no overlapping scopes
  const steps = carryForward(ordered.map(c => ({
    source: c.source,
    points: [...c.scans].sort(compareScans).map(s => ({ at: s.observed_through, value: s })),
  })))
  return ordered.map((c, k) => ({
    source: c.source,
    points: steps.map(({ at, values }) => ({ at, bytes: values[k]?.total_bytes ?? null, objects: values[k]?.total_objects ?? null })),
  }))
}
