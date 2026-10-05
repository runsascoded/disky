/** Any list of prefixes at one scan, with the numbers a table row shows
 * (`TreeNode`'s wire names) — `/staged`'s items, the action log's prefixes —
 * from point lookups on the floor-free index (the same `readAsks` path owner
 * totals take), never one subtree read per prefix. */
import type { Env } from './auth.js'
import { idxKey } from './claims.js'
import { type Ask, columnsFor, openIndex, readAsks, type Row } from './index.js'

/** One prefix's numbers, in the wire's names (`TreeNode`): bytes, objects,
 *  size-weighted mean created day, last-read day, per-owner bytes (desc),
 *  non-STANDARD class bytes. */
export interface PrefixStat {
  b: number
  o: number
  d?: number
  a?: number
  us?: [string, number][]
  cb?: Record<string, number>
}

const FIELDS: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'mtime_mean', 'mtime_w', 'last_read', 'cls2', 'cls3', 'cls4']
type StatRow = Pick<Row, 'path' | 'depth' | 'usr' | 'size' | 'n_files' | 'mtime_mean' | 'mtime_w' | 'last_read'> & Partial<Pick<Row, 'cls2' | 'cls3' | 'cls4'>>

/** The most prefixes one request sizes. */
export const MAX_PREFIXES = 1000

/** Fold index rows (one per path × owner slice) into per-prefix stats, keyed
 * by the prefix as given. A prefix with no row (gone by this scan) is absent. */
export function foldPrefixes(prefixes: string[], rows: StatRow[]): Record<string, PrefixStat> {
  const byKey = new Map<string, string[]>()
  for (const p of prefixes) {
    const { path, depth } = idxKey(p)
    const k = `${depth}\t${path}`
    byKey.set(k, [...(byKey.get(k) ?? []), p])
  }
  const acc = new Map<string, { b: number; o: number; wts: number; wb: number; a: number | null; ub: Map<string, number>; cb: Map<string, number> }>()
  for (const r of rows) {
    const k = `${r.depth}\t${r.path}`
    if (!byKey.has(k)) continue
    let a = acc.get(k)
    if (!a) acc.set(k, (a = { b: 0, o: 0, wts: 0, wb: 0, a: null, ub: new Map(), cb: new Map() }))
    a.b += r.size
    a.o += r.n_files
    if (r.mtime_mean != null && r.mtime_w > 0) { a.wts += r.mtime_mean * r.mtime_w; a.wb += r.mtime_w }
    if (r.last_read != null) a.a = a.a == null ? r.last_read : Math.max(a.a, r.last_read)
    if (r.usr) a.ub.set(r.usr, (a.ub.get(r.usr) ?? 0) + r.size)
    for (const [c, v] of [['2', r.cls2], ['3', r.cls3], ['4', r.cls4]] as [string, number | undefined][]) if (v) a.cb.set(c, (a.cb.get(c) ?? 0) + v)
  }
  const out: Record<string, PrefixStat> = {}
  for (const [k, a] of acc) {
    const s: PrefixStat = { b: a.b, o: a.o }
    if (a.wb) s.d = Math.round(a.wts / a.wb / 86400)
    if (a.a != null) s.a = a.a
    if (a.ub.size) s.us = [...a.ub].sort((x, y) => y[1] - x[1])
    if (a.cb.size) s.cb = Object.fromEntries([...a.cb].sort((x, y) => y[1] - x[1]))
    for (const p of byKey.get(k)!) out[p] = s
  }
  return out
}

export async function prefixesAt(env: Env, date: string, prefixes: string[]): Promise<{ stats: Record<string, PrefixStat>; groups: number }> {
  const idx = await openIndex(env, date)
  const want = new Map<string, number>()
  for (const p of prefixes) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  const asks: Ask[] = [...want].map(([path, depth]) => ({ depth, path }))
  const { rows, groups } = await readAsks(idx, asks, r => want.get(r.path) === r.depth, {
    columns: columnsFor(idx, FIELDS),
    maxGroups: Math.max(400, 2 * asks.length),
  })
  return { stats: foldPrefixes(prefixes, rows), groups }
}
