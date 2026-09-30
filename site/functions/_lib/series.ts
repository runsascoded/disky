// The pure parts of `/api/series?split=roots` (specs/root-geneses.md §1):
// per-root traces from the depth-1 rows of each indexed scan, plus what a
// tier-less scan's meta.json can still say about its roots.

export interface Pt { date: string; b: number; o: number }
export interface RootRow { path: string; b: number; o: number }
export interface RootTrace { path: string; points: Pt[] }

/** A meta.json's per-root totals: `buckets` (multi-bucket scans), else the
 * flat total attributed to `soleRoot` when the store is known to have had
 * exactly one root (a single-bucket store's history), else nothing. */
export function metaRoots(m: { total_bytes?: number; total_objects?: number; buckets?: Record<string, { total_bytes: number; total_objects: number }> }, soleRoot: string | null): RootRow[] {
  if (m.buckets) return Object.entries(m.buckets).map(([path, v]) => ({ path, b: v.total_bytes, o: v.total_objects }))
  if (soleRoot != null && typeof m.total_bytes === 'number') return [{ path: soleRoot, b: m.total_bytes, o: m.total_objects ?? 0 }]
  return []
}

/** Per-root traces from `(date → depth-1 rows)`, ordered by first
 * appearance, then bytes desc at the latest date. Dates are sorted; a root
 * absent from a scan has no point there (its band starts at its genesis). */
export function rootPoints(byDate: Map<string, RootRow[]>): RootTrace[] {
  const dates = [...byDate.keys()].sort()
  const traces = new Map<string, Pt[]>()
  for (const date of dates) {
    for (const r of byDate.get(date)!) {
      let pts = traces.get(r.path)
      if (!pts) traces.set(r.path, (pts = []))
      pts.push({ date, b: r.b, o: r.o })
    }
  }
  const last = dates[dates.length - 1]
  const latest = new Map((byDate.get(last) ?? []).map(r => [r.path, r.b]))
  return [...traces.entries()]
    .map(([path, points]) => ({ path, points }))
    .sort((a, b) => a.points[0].date.localeCompare(b.points[0].date) || (latest.get(b.path) ?? 0) - (latest.get(a.path) ?? 0) || a.path.localeCompare(b.path))
}
