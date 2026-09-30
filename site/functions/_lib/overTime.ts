// The cross-scan over-time index reader (specs/obs-axis-indexing.md Phase 1):
// a path's size-over-time line stitched across sealed capped-K multi-scan
// groups, replacing `/api/series`'s one point read per scan generation.
//
// Routing + expansion are pyrmts primitives; cw supplies only the range IO.
// `MultiScanD1Index.listMultiScans('over-time')` reads the group manifest
// (`pyramid_multiscans`); `seriesAcrossGroups` orders the groups by scan span
// and, per group, calls cw's `load` — a *footer-pruned* `readPoint` of just the
// queried `(depth,path)` row groups, returned as a key-filtered partial
// `MultiScan` (that key's interval rows + the group's full `scans` list, which
// is the load-bearing part). So a line is ⌈N/K⌉ pruned reads, not one giant
// file; the unsealed ≤K-scan tip is served by `/api/series`'s per-scan fallback.
import type { Env } from './auth.js'
import { openIndex, readPoint } from './index.js'
import { type Dim, type Metric, type MultiScan, type MultiScanIndexEntry, type Pyramid, seriesAcrossGroups } from 'pyrmts'
import { MultiScanD1Index } from 'pyrmts-cfw'
import { PRIMARY_STORE, storeKey } from './stores.js'

const COLS = ['depth', 'path', 'b', 'o', '__scan_lo', '__scan_hi']

/** The store's manifest dataset: `over-time` for the primary, `<store>:over-time`
 * for a secondary store (pyrmts owns `pyramid_multiscans`, whose `(dataset,
 * key)` PK already namespaces it; `dt_cloud.overtime.multiscan_dataset`). */
export const overTimeDataset = (env: Env): string => {
  const s = storeKey(env)
  return s === PRIMARY_STORE ? 'over-time' : `${s}:over-time`
}

// cw's over-time shape as a pyrmts logical schema: key `(depth, path)`, state
// `(b, o)` (mirrors `over_time_pyramid()` in the producer).
type Schema = Pick<Pyramid, 'binCol' | 'dims' | 'metrics'>
const SCHEMA: Schema = {
  binCol: 'depth',
  dims: [{ name: 'path', type: 'string' } as Dim],
  metrics: [{ name: 'b', monoid: 'count' } as Metric, { name: 'o', monoid: 'count' } as Metric],
}

/** A path's line from the over-time groups: `points` (scan → {b,o}, absent
 * scans dropped so the chart keeps gap semantics) and `covered`, every scan the
 * sealed groups span. The groups are built from the floor-free path index, so a
 * covered scan missing from `points` is a *known* absence — not a reason to
 * re-read that scan (which made a path newer than most of the history pay one
 * per-scan read per old scan: 23 s cold for an absent path, 1.2 min for a
 * five-root filter). null = no groups synced / unreadable → per-scan reads. */
export interface OverTime {
  points: Map<string, { b: number; o: number }>
  covered: Set<string>
}

export async function readOverTime(env: Env, path: string): Promise<OverTime | null> {
  if (!env.DB) return null
  let entries: MultiScanIndexEntry[]
  try {
    entries = await new MultiScanD1Index(env.DB).listMultiScans(overTimeDataset(env))
  } catch {
    return null // manifest table absent (not migrated) → fallback
  }
  if (!entries.length) return null
  const depth = path === '' ? 0 : path.split('/').length
  // load(archiveKey): footer-pruned fetch of one group's rows for this key, as a
  // partial MultiScan. The group's dir/date is its archive key (how the producer
  // synced each group's footer); its scans list comes from the manifest entry.
  const byKey = new Map(entries.map(e => [e.key, e]))
  const load = async (archiveKey: string): Promise<MultiScan> => {
    const entry = byKey.get(archiveKey)
    if (!entry) return { rows: [], scans: [], encoder: 'interval' }
    const rows = await readPoint(await openIndex(env, archiveKey, 'over-time'), depth, path, COLS)
    // Parquet int64 columns (`depth` as DuckDB wrote it, `b`/`o`, the scan
    // bounds) arrive as BigInt; pyrmts' own reader normalizes to number and
    // its key/interval code `JSON.stringify`s the key, which throws on BigInt
    // ("Do not know how to serialize a BigInt" — every plain-path series 500'd
    // for the first hours the groups existed, 2026-09-28).
    return { rows: rows.map(plainRow) as MultiScan['rows'], scans: entry.scans, encoder: 'interval' }
  }
  let pts: Awaited<ReturnType<typeof seriesAcrossGroups>>
  try {
    // `seriesAcrossGroups` awaits each group's `load` in turn (it only needs
    // them in span order); start every group's read up front so the ⌈N/K⌉
    // D1 + range round trips overlap instead of stacking.
    const loaded = new Map(entries.map(e => {
      const p = load(e.key)
      p.catch(() => {}) // still rejects where awaited; just not "unhandled" before then
      return [e.key, p] as const
    }))
    pts = await seriesAcrossGroups(entries, SCHEMA, { depth, path }, key => loaded.get(key) ?? load(key))
  } catch (e) {
    // A broken or half-published group must degrade to the per-scan reads,
    // never fail the chart.
    console.log(`over-time: falling back to per-scan reads for ${path || '/'}: ${(e as Error).message}`)
    return null
  }
  const points = new Map<string, { b: number; o: number }>()
  for (const p of pts) {
    const b = num(p.state.b)
    const o = num(p.state.o)
    if (b !== 0 || o !== 0) points.set(p.scan, { b, o })
  }
  return { points, covered: new Set(entries.flatMap(e => e.scans)) }
}

/** One scan's point summed over `lines` (one per match root; a plain path is a
 * single line): `undefined` when any line's groups don't cover the scan (the
 * caller reads it per scan), `null` for a covered scan where every root is
 * absent, else the sum. */
export function overTimePoint(lines: OverTime[], date: string): { b: number; o: number } | null | undefined {
  if (!lines.length || !lines.every(l => l.covered.has(date))) return undefined
  let b = 0, o = 0, any = false
  for (const l of lines) {
    const pt = l.points.get(date)
    if (pt) { b += pt.b; o += pt.o; any = true }
  }
  return any ? { b, o } : null
}

const num = (v: unknown): number => (typeof v === 'bigint' ? Number(v) : (v as number) ?? 0)

/** A row with every BigInt field as a number (the shape pyrmts' own parquet
 * reader produces). */
export function plainRow(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const k in row) {
    const v = row[k]
    out[k] = typeof v === 'bigint' ? Number(v) : v
  }
  return out
}
