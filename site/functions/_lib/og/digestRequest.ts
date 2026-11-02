/** `POST /api/digest-image`'s request, validated (pure; `api/digest-image.ts` resolves a `usage` diff and renders):
 *
 *   {kind?: 'usage', title, note, month: 'YYYY-MM', from?: 'YYYY-MM-DD', today_at?: [p0, p1], points: [{scan, bytes}],
 *    quota?: {bytes, name}, diff?: {from, to, path, title}, units?: 'TiB' | 'TB'}
 *     → `digestSvg`: the usage line against its quota, over the headline interval's diff map (the cw template)
 *   {kind: 'pools', title, note, month, from?, today_at?, alert_pct?, pools: [{name, quota, points: [{scan, bytes}]}],
 *    diff?, units?}
 *     → `poolsSvg`: every quota pool's share of its quota (one line each, the quota a rule at 100%), over the diff map
 *       (the cw template's quota-pressure plot, specs/digest-quota-pressure.md)
 *   {kind: 'tiers', title, note, month, from?, tiers: [name, …] (bottom → top), points: [{scan, bytes: [per tier]}],
 *    diff?: {from, to, path, title}, units?: 'TiB' | 'TB'}
 *     → `tiersSvg`: the total line over the stacked storage classes, over the headline interval's diff map (the gcs
 *       template)
 *
 * A body without `kind` is `usage` (the cw job's request predates the field). `from`: the OP window's first day
 * (specs/digest-rolling-window.md), before the month on all but its last days: the axis starts there (or at the
 * baseline scan before it), the latest scan sits at `todayAt` of the width, and the card shades the month. Without
 * it, the axis is the month. Either kind's `diff` is resolved by the endpoint (`buildDiff`). */
import { type DigestPoint, type DigestUnits, MAX_POOLS, type PoolSeries, TIER_COLORS, type TierPoint } from './digestCard.js'
import { isScanId, scanTime } from '../../../src/scanSlug.js'

/** The most tiers a card stacks (one default colour each). */
export const MAX_TIERS = TIER_COLORS.length

export interface DiffSpec {
  from: string
  to: string
  path: string
  title: string
}

interface Common {
  title: string
  note: string
  /** The x axis' span, epoch ms: the month (scans fill it in left → right), widened back to the window's first day
   *  (`from`) and a lead-in scan before it. */
  from: number
  to: number
  /** The month's first instant, epoch ms (the shaded current month starts there). */
  month: number
  units: DigestUnits
}

export type DigestRequest =
  | Common & {
    kind: 'usage'
    points: DigestPoint[]
    quota: { bytes: number; name: string } | null
    diff: DiffSpec | null
  }
  | Common & { kind: 'tiers'; tiers: { name: string; color: string }[]; points: TierPoint[]; diff: DiffSpec | null }
  | Common & { kind: 'pools'; pools: PoolSeries[]; alertPct: number; diff: DiffSpec | null }

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const isBytes = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0

/** A request's `diff` (absent: null), or the 400's message. */
function parseDiff(d: unknown): DiffSpec | null | string {
  if (d === undefined || d === null) return null
  if (!isObj(d)) return 'diff: from < to, scan ids'
  const { from: f, to: t, path, title } = d
  if (!isScanId(f) || !isScanId(t) || f >= t) return 'diff: from < to, scan ids'
  if (typeof path !== 'string' || path.includes('..') || path.startsWith('/')) return 'diff: bad path'
  if (typeof title !== 'string') return 'diff: title required'
  return { from: f, to: t, path, title }
}

/** Where the latest scan sits across a window card's width: 0.7 on the month's 1st, rising linearly to 1 on its last
 *  day (`days` in the month), so an early thread has some runway to its right and a late one ends at the latest scan. */
export function todayAt(day: number, days: number, [p0, p1]: [number, number] = TODAY_AT): number {
  return p0 + ((p1 - p0) * (Math.min(day, days) - 1)) / (days - 1)
}

/** `todayAt`'s range by default: 0.7 on the 1st to 1 on the last day. A request's `today_at` overrides it (the
 *  `/admin/slack` preview's candidates). */
export const TODAY_AT: [number, number] = [0.7, 1]

/** A window card's x span (specs/digest-rolling-window.md): from the window's first day (`start`, epoch ms) or its
 *  baseline scan before it, whichever is earlier, to the point that puts the latest scan (`ts`' last) at `todayAt`
 *  of the width: `today + (today − from) · (1 − p) / p`. */
export function windowFrame(start: number, ts: number[], y: number, mo: number, range: [number, number] = TODAY_AT): { from: number; to: number } {
  const today = Math.max(...ts)
  const from = Math.min(start, ...ts)
  const p = todayAt(new Date(today).getUTCDate(), new Date(Date.UTC(y, mo + 1, 0)).getUTCDate(), range)
  return { from, to: today + ((today - from) * (1 - p)) / p }
}

/** The request, or the 400's message. */
export function parseDigestRequest(b: unknown): DigestRequest | string {
  if (!isObj(b) || typeof b.title !== 'string' || typeof b.note !== 'string') return 'title and note required'
  const kind = b.kind ?? 'usage'
  if (kind !== 'usage' && kind !== 'tiers' && kind !== 'pools') return "kind: 'usage' | 'tiers' | 'pools'"
  const m = typeof b.month === 'string' ? /^(\d{4})-(\d{2})$/.exec(b.month) : null
  if (!m) return 'month: YYYY-MM'
  if (b.units !== undefined && b.units !== 'TiB' && b.units !== 'TB') return "units: 'TiB' | 'TB'"
  const units = (b.units ?? 'TiB') as DigestUnits
  const raw = b.points ?? []
  if (!Array.isArray(raw)) return 'points: an array'
  const w = b.from === undefined ? null : typeof b.from === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(b.from) : null
  if (b.from !== undefined && !w) return 'from: YYYY-MM-DD'
  const ta = b.today_at
  const okAt = ta === undefined || (Array.isArray(ta) && ta.length === 2 && ta.every(v => typeof v === 'number') && 0 < ta[0] && ta[0] <= ta[1] && ta[1] <= 1)
  if (!okAt) return 'today_at: [p0, p1], 0 < p0 ≤ p1 ≤ 1'
  const frame = (ts: number[]) => {
    const [y, mo] = [Number(m[1]), Number(m[2]) - 1]
    const month = Date.UTC(y, mo, 1)
    if (!w || !ts.length) return { from: Math.min(month - 16 * 3600_000, ...ts), to: Date.UTC(y, mo + 1, 1) - 8 * 3600_000, month }
    return { ...windowFrame(Date.UTC(Number(w[1]), Number(w[2]) - 1, Number(w[3])), ts, y, mo, (ta as [number, number] | undefined) ?? TODAY_AT), month }
  }
  const diff = parseDiff(b.diff)
  if (typeof diff === 'string') return diff
  const common = { title: b.title, note: b.note, units }
  if (kind === 'tiers') {
    const names = b.tiers
    if (!Array.isArray(names) || !names.length || names.length > MAX_TIERS || !names.every(n => typeof n === 'string')) {
      return `tiers: 1–${MAX_TIERS} names, bottom → top`
    }
    const points: TierPoint[] = []
    for (const p of raw) {
      if (!isObj(p) || !isScanId(p.scan) || !Array.isArray(p.bytes) || p.bytes.length !== names.length || !p.bytes.every(isBytes)) {
        return 'points: [{scan, bytes: [one per tier]}]'
      }
      points.push({ t: scanTime(p.scan), bytes: p.bytes })
    }
    points.sort((x, y) => x.t - y.t)
    const tiers = (names as string[]).map((name, i) => ({ name, color: TIER_COLORS[i] }))
    return { kind, ...common, ...frame(points.map(p => p.t)), tiers, points, diff }
  }
  if (kind === 'pools') {
    const ps = b.pools
    if (!Array.isArray(ps) || !ps.length || ps.length > MAX_POOLS) return `pools: 1–${MAX_POOLS} [{name, quota, points: [{scan, bytes}]}]`
    const pools: PoolSeries[] = []
    for (const pl of ps) {
      if (!isObj(pl) || typeof pl.name !== 'string' || !isBytes(pl.quota) || !pl.quota || !Array.isArray(pl.points)) {
        return 'pools: [{name, quota > 0, points: [{scan, bytes}]}]'
      }
      const pts: DigestPoint[] = []
      for (const p of pl.points) {
        if (!isObj(p) || !isScanId(p.scan) || !isBytes(p.bytes)) return 'pools: [{name, quota > 0, points: [{scan, bytes}]}]'
        pts.push({ t: scanTime(p.scan), bytes: p.bytes })
      }
      pools.push({ name: pl.name, quota: pl.quota, points: pts.sort((x, y) => x.t - y.t) })
    }
    const a = b.alert_pct ?? 90
    if (typeof a !== 'number' || !(a > 0 && a <= 100)) return 'alert_pct: (0, 100]'
    return { kind, ...common, ...frame(pools.flatMap(pl => pl.points.map(p => p.t))), pools, alertPct: a, diff }
  }
  const points: DigestPoint[] = []
  for (const p of raw) {
    if (!isObj(p) || !isScanId(p.scan) || typeof p.bytes !== 'number') return 'points: [{scan, bytes}]'
    points.push({ t: scanTime(p.scan), bytes: p.bytes })
  }
  points.sort((x, y) => x.t - y.t)
  const q = b.quota
  const quota = isObj(q) && typeof q.bytes === 'number' && typeof q.name === 'string' ? { bytes: q.bytes, name: q.name } : null
  return { kind, ...common, ...frame(points.map(p => p.t)), points, quota, diff }
}
