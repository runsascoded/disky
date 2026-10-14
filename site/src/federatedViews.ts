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

/** Source traces at union event times. Null before the first observation, and
 * carry forward afterward. Consumers draw steps, not interpolated growth. */
export function federatedSeries(catalogs: readonly SourceCatalog[]): FederatedSeries[] {
  const events = federatedEvents(catalogs)
  return [...catalogs].sort((a, b) => a.source < b.source ? -1 : a.source > b.source ? 1 : 0).map(c => {
    const scans = [...c.scans].sort(compareScans)
    let cursor = -1
    const points = events.map(({ at }) => {
      while (cursor + 1 < scans.length && scans[cursor + 1].observed_through <= at) cursor++
      const scan = scans[cursor]
      return { at, bytes: scan?.total_bytes ?? null, objects: scan?.total_objects ?? null }
    })
    return { source: c.source, points }
  })
}
