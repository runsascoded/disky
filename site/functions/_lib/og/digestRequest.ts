/** `POST /api/digest-image`'s request, validated (pure; `api/digest-image.ts` resolves a `usage` diff and renders):
 *
 *   {kind?: 'usage', title, note, month: 'YYYY-MM', points: [{scan, bytes}],
 *    quota?: {bytes, name}, diff?: {from, to, path, title}, units?: 'TiB' | 'TB'}
 *     → `digestSvg`: the usage line against its quota, over the headline interval's diff map (the cw template)
 *   {kind: 'tiers', title, note, month, tiers: [name, …] (bottom → top), points: [{scan, bytes: [per tier]}],
 *    units?: 'TiB' | 'TB'}
 *     → `tiersSvg`: the total line over the stacked storage classes (the gcs template)
 *
 * A body without `kind` is `usage` (the cw job's request predates the field). */
import { type DigestPoint, type DigestUnits, TIER_COLORS, type TierPoint } from './digestCard.js'
import { isScanId, scanTime } from '../../../src/scanSlug.js'

/** The most tiers a card stacks (one default colour each). */
export const MAX_TIERS = TIER_COLORS.length

interface Common {
  title: string
  note: string
  /** The x axis' span, epoch ms: the month (scans fill it in left → right), widened to a lead-in scan before it. */
  from: number
  to: number
  units: DigestUnits
}

export type DigestRequest =
  | Common & {
    kind: 'usage'
    points: DigestPoint[]
    quota: { bytes: number; name: string } | null
    diff: { from: string; to: string; path: string; title: string } | null
  }
  | Common & { kind: 'tiers'; tiers: { name: string; color: string }[]; points: TierPoint[] }

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const isBytes = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0

/** The request, or the 400's message. */
export function parseDigestRequest(b: unknown): DigestRequest | string {
  if (!isObj(b) || typeof b.title !== 'string' || typeof b.note !== 'string') return 'title and note required'
  const kind = b.kind ?? 'usage'
  if (kind !== 'usage' && kind !== 'tiers') return "kind: 'usage' | 'tiers'"
  const m = typeof b.month === 'string' ? /^(\d{4})-(\d{2})$/.exec(b.month) : null
  if (!m) return 'month: YYYY-MM'
  if (b.units !== undefined && b.units !== 'TiB' && b.units !== 'TB') return "units: 'TiB' | 'TB'"
  const units = (b.units ?? 'TiB') as DigestUnits
  const raw = b.points ?? []
  if (!Array.isArray(raw)) return 'points: an array'
  const frame = (ts: number[]) => {
    const [y, mo] = [Number(m[1]), Number(m[2]) - 1]
    return { from: Math.min(Date.UTC(y, mo, 1) - 16 * 3600_000, ...ts), to: Date.UTC(y, mo + 1, 1) - 8 * 3600_000 }
  }
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
    return { kind, ...common, ...frame(points.map(p => p.t)), tiers, points }
  }
  const points: DigestPoint[] = []
  for (const p of raw) {
    if (!isObj(p) || !isScanId(p.scan) || typeof p.bytes !== 'number') return 'points: [{scan, bytes}]'
    points.push({ t: scanTime(p.scan), bytes: p.bytes })
  }
  points.sort((x, y) => x.t - y.t)
  const q = b.quota
  const quota = isObj(q) && typeof q.bytes === 'number' && typeof q.name === 'string' ? { bytes: q.bytes, name: q.name } : null
  let diff = null
  if (b.diff) {
    if (!isObj(b.diff)) return 'diff: from < to, scan ids'
    const { from: f, to: t, path, title } = b.diff
    if (!isScanId(f) || !isScanId(t) || f >= t) return 'diff: from < to, scan ids'
    if (typeof path !== 'string' || path.includes('..') || path.startsWith('/')) return 'diff: bad path'
    if (typeof title !== 'string') return 'diff: title required'
    diff = { from: f, to: t, path, title }
  }
  return { kind, ...common, ...frame(points.map(p => p.t)), points, quota, diff }
}
