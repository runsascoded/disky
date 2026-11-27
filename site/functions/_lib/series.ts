// The pure parts of `/api/series?split=roots` (specs/done/root-geneses.md §1):
// per-root traces from the depth-1 rows of each indexed scan, plus what a
// tier-less scan's meta.json can still say about its roots.

/** A scan's change flows at one path (specs/change-metrics.md §3.1, `interval_changes`): the objects it added, deleted
 *  and modified since the scan before it, and their bytes (`modified_bytes`: the rewritten objects' new sizes). */
export interface Flows { added: number; deleted: number; modified: number; added_bytes: number; deleted_bytes: number; modified_bytes: number }
export const FLOW_KEYS = ['added', 'deleted', 'modified', 'added_bytes', 'deleted_bytes', 'modified_bytes'] as const
/** How a scan's mods were decided: both version tokens, else the size, else the mtime. */
export interface ModsBy { token: number; size: number; mtime: number }
/** A scan's counters as the store holds them (`changes.json`, a manifest's `changes`): per path (the root `''`, and
 *  each depth-1 path where something changed), against `since`, the scan before it. */
export interface ChangeEntry { since: string; mtime: string; paths: Record<string, Flows & { mods_by: ModsBy }> }

export interface Pt { date: string; b: number; o: number; ch?: Flows; since?: string }
export interface RootRow { path: string; b: number; o: number }
export interface RootTrace { path: string; points: Pt[] }

/** A point as `/api/series` serves it (specs/descriptive-field-names.md §3d): a union root's names the members
 *  it doesn't cover yet (`missing`). The root's and depth-1 paths' points carry the scan's change flows where the
 *  store counted them (`Flows`; specs/change-metrics.md §4.1), and `since`, the scan they're against, where that isn't
 *  the point before. */
export interface SeriesPoint extends Partial<Flows> { date: string; bytes: number; objects: number; since?: string; missing?: string[] }
/** A root's trace as `/api/series?split=roots` serves it. */
export interface SeriesTrace { path: string; points: SeriesPoint[] }
/** The readers' own `{ date, b, o }` as a response's point. */
export const wirePoint = (p: Pt & { missing?: string[] }): SeriesPoint =>
  ({ date: p.date, bytes: p.b, objects: p.o, ...(p.ch ?? {}), ...(p.since ? { since: p.since } : {}), ...(p.missing ? { missing: p.missing } : {}) })

/** `entry`'s flows at `path`: its counters there, zero where nothing changed under it. */
export function flowsAt(entry: ChangeEntry, path: string): Flows {
  const c = entry.paths[path]
  return Object.fromEntries(FLOW_KEYS.map(k => [k, c?.[k] ?? 0])) as unknown as Flows
}

/** `points` (one path's series, oldest first) with each counted scan's flows at `path` (`changes`, scan → entry), and
 *  `since` where the scan they're against isn't the point before (a scan the series lacks: its changes fold into the
 *  next). A scan without counters keeps no flow fields: absent, not zero. */
export function withFlows(points: readonly Pt[], path: string, changes: ReadonlyMap<string, ChangeEntry>): Pt[] {
  return points.map((p, i) => {
    const e = changes.get(p.date)
    if (!e) return p
    return { ...p, ch: flowsAt(e, path), ...(e.since !== points[i - 1]?.date ? { since: e.since } : {}) }
  })
}

/** The window's counting record (`/api/series`' `changes`): its first counted scan, and how its mods at `path` were
 *  decided, summed (a page footnotes the share decided by size or mtime). Null without a counted scan. */
export function changesNote(points: readonly Pt[], path: string, changes: ReadonlyMap<string, ChangeEntry>): { from: string; mods_by: ModsBy } | null {
  const counted = points.filter(p => changes.has(p.date))
  if (!counted.length) return null
  const by: ModsBy = { token: 0, size: 0, mtime: 0 }
  for (const p of counted) {
    const m = changes.get(p.date)!.paths[path]?.mods_by
    if (m) { by.token += m.token; by.size += m.size; by.mtime += m.mtime }
  }
  return { from: counted[0].date, mods_by: by }
}

/** Flows summed (a group's members, a union's step): present only when every summand has them. */
export function sumFlows(fs: readonly (Partial<Flows> | null | undefined)[]): Flows | null {
  if (!fs.length || fs.some(f => !f || f.added === undefined)) return null
  return Object.fromEntries(FLOW_KEYS.map(k => [k, fs.reduce((n, f) => n + (f![k] ?? 0), 0)])) as unknown as Flows
}

/** A union step's flows (`carryForward`'s step at `at`, `values` each member's point there): the members that scanned
 *  at that instant (their own point is at `at`), summed. A carried point's flows are its own scan's, already counted at
 *  its step: never again. Null when no member scanned there with flows, or one that did has none. */
export function stepFlows<T extends SeriesPoint>(at: string, values: readonly (T | null)[], traces: readonly { points: readonly { at: string; value: T }[] }[]): Flows | null {
  return sumFlows(values.filter((v, i) => v && traces[i].points.some(p => p.at === at && p.value === v)).map(v => pointFlows(v!)))
}

/** A response point's flows, if it has them. */
export const pointFlows = (p: SeriesPoint): Flows | null =>
  p.added === undefined ? null : Object.fromEntries(FLOW_KEYS.map(k => [k, p[k] ?? 0])) as unknown as Flows
export const wireTraces = (ts: readonly RootTrace[]): SeriesTrace[] => ts.map(t => ({ path: t.path, points: t.points.map(wirePoint) }))

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
