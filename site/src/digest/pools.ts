/** Quota pools for the `cw` template's quota-pressure variants (specs/digest-quota-pressure.md): each quota's used
 * share per scan, buckets sharing a `zone` summed against the zone's quota (as `digest_cw._groups`), sorted by
 * pressure; and the OP's quota line. Pure; DOM-free. */
import type { CwScan } from './cw'
import { deg, type DigestCfg, type Quota, type Shape } from './common'
import { scanTime } from '../scanSlug'
import { fixed } from './pyfmt'

export interface Pool {
  /** The bucket's label, or the zone's name for buckets sharing one. */
  name: string
  /** The member buckets (config order). */
  buckets: string[]
  quota: Quota
  /** Its `runway-hue` mark; a zone's members all state the same one (`digest._check_zones`). */
  shape: Shape
}

/** The config's pools: one per quota of each bucket without a zone, one per zone (its first member's quotas and shape). */
export function pools(cfg: DigestCfg): Pool[] {
  const out: Pool[] = []
  const zones = new Map<string, string[]>()
  for (const [b, c] of Object.entries(cfg.buckets)) {
    if (!c.quotas.length) continue
    if (c.zone) zones.set(c.zone, [...(zones.get(c.zone) ?? []), b])
    else for (const q of c.quotas) out.push({ name: c.quotas.length > 1 ? `${c.label || b} (${q.short ?? q.name})` : c.label || b, buckets: [b], quota: q, shape: c.shape ?? 'circle' })
  }
  for (const [z, bs] of zones) for (const q of cfg.buckets[bs[0]].quotas) out.push({ name: bs.length > 1 ? `${z} zone` : cfg.buckets[bs[0]].label || bs[0], buckets: bs, quota: q, shape: cfg.buckets[bs[0]].shape ?? 'circle' })
  return out
}

/** A pool's used size at a scan, in the config's unit: the primary's size under the quota's classes, the others'
 *  sizes as listed (a bucket the scan lacks counts 0); null when the scan has none of its buckets. */
export function used(p: Pool, r: CwScan, cfg: DigestCfg): number | null {
  let s = 0
  let any = false
  for (const b of p.buckets) {
    if (b === cfg.primary) {
      any = true
      if (!p.quota.classes.length) s += r.tb
      else for (const c of p.quota.classes) s += r.classes?.[String(c)] ?? 0
    } else if (b in r.extra) {
      any = true
      s += r.extra[b]
    }
  }
  return any ? s : null
}

/** Used share of the quota, percent. */
export const pctOf = (p: Pool, v: number, cfg: DigestCfg): number => (v / (p.quota.bytes / cfg.unit.bytes)) * 100

/** The pressure mark for a share, every pool one: red at or over `alertAt` (or the quota), orange within
 *  `ORANGE_PTS` below it, yellow within `YELLOW_PTS`, green under that (`digest_cw._mark`'s scale). */
export const ORANGE_PTS = 10
export const YELLOW_PTS = 40
export function mark(pct: number, alertAt: number): string {
  if (pct >= Math.min(alertAt, 100)) return ':red_circle: '
  if (pct >= alertAt - ORANGE_PTS) return ':large_orange_circle: '
  if (pct >= alertAt - YELLOW_PTS) return ':large_yellow_circle: '
  return ':large_green_circle: '
}

/** Below this share, a pool's clause is just its name and whole percent. */
export const QUIET_PCT = 25

/** `quotas: 'runway'` (specs/digest-runway-marks.md §2; `digest_cw.RUNWAY_*` / `_runway_mark` / `_runway_text`, the
 *  replies' rule — keep both identical): growth is the fastest net fill per day over these look-backs (days), each from
 *  the latest scan at or before it. */
export const RUNWAY_WINDOWS_D = [1, 3, 7]
/** 🔴 under `RUNWAY_RED_D` days to full (or over quota), 🟠 under `RUNWAY_ORANGE_D` (or at least `RUNWAY_FULL_PCT`
 *  full), 🟡 under `RUNWAY_YELLOW_D`, 🟢 otherwise; a clause under `RUNWAY_TEXT_D` days also shows its rate and runway. */
export const RUNWAY_RED_D = 3
export const RUNWAY_ORANGE_D = 14
export const RUNWAY_YELLOW_D = 30
export const RUNWAY_TEXT_D = 60
export const RUNWAY_FULL_PCT = 95

/** Days to full at `growth` (unit/day; null: no earlier scan in reach): Infinity when not growing, 0 at or over quota. */
export interface Runway { days: number; growth: number | null }

/** The fastest net fill per day reaching `v` at `t` (ms) over `RUNWAY_WINDOWS_D`, from `hist` ([ms, value], in
 *  order); null when no window has a point. */
export function growth(hist: [number, number][], t: number, v: number): number | null {
  const rates: number[] = []
  for (const w of RUNWAY_WINDOWS_D) {
    const then = hist.filter(([x]) => x <= t - w * 86400_000)
    if (!then.length) continue
    const [t0, v0] = then[then.length - 1]
    rates.push((v - v0) / ((t - t0) / 86400_000))
  }
  return rates.length ? Math.max(...rates) : null
}

export function runway(free: number, g: number | null): Runway {
  if (free <= 0) return { days: 0, growth: g }
  return { days: g != null && g > 0 ? free / g : Infinity, growth: g }
}

export function runwayMark(r: Runway, pct: number): string {
  if (r.days < RUNWAY_RED_D) return ':red_circle: '
  if (r.days < RUNWAY_ORANGE_D || pct >= RUNWAY_FULL_PCT) return ':large_orange_circle: '
  return r.days < RUNWAY_YELLOW_D ? ':large_yellow_circle: ' : ':large_green_circle: '
}

/** `quotas: 'runway-hue'` (specs/digest-runway-marks.md §3; `digest_cw.RUNWAY_HUE_*` / `_runway_hue`): the workspace
 *  emoji `:runway-1:` (red) … `:runway-8:` (green), one more than the cut points (days to full) the runway reaches;
 *  a pool ≥ `RUNWAY_FULL_PCT` full is never calmer than `RUNWAY_HUE_FULL_N`. A pool with a shape gets
 *  `:runway-<shape>-N:`, the same step in the same color; a circle keeps the plain `:runway-N:`. */
export const RUNWAY_HUE_D = [1.8, 3, 6, 10, 19, 33, 60]
export const RUNWAY_HUE_FULL_N = 3

function hueStep(r: Runway, pct: number): number {
  const n = 1 + RUNWAY_HUE_D.filter(c => r.days >= c).length
  return pct >= RUNWAY_FULL_PCT ? Math.min(n, RUNWAY_HUE_FULL_N) : n
}

export function runwayHue(r: Runway, pct: number, shape: Shape = 'circle'): string {
  return `:runway-${shape === 'circle' ? '' : `${shape}-`}${hueStep(r, pct)}: `
}

/** Candidate marks, previewed on `/admin/slack` only (no emoji uploaded, and the job's params refuse them): the angle
 *  is the weekly-normalized rate driving the runway, as the trend arrows' scale reads a percent; the color its
 *  `runway-hue` step. `arrow`: a bare arrow in that color; `arrow-disc` / `arrow-chip`: white on a colored disc / rounded
 *  square; `arrow-circle`: today's trend arrow beside a `:runway-N:` circle. */
export type ArrowMark = 'arrow' | 'arrow-disc' | 'arrow-chip' | 'arrow-circle'
export const ARROW_MARKS: ArrowMark[] = ['arrow', 'arrow-disc', 'arrow-chip', 'arrow-circle']
/** Every mark over a pool's runway. */
export type PoolMark = 'runway' | 'runway-hue' | ArrowMark

const weeklyDeg = (r: Runway, v: number): number => deg(r.growth != null && v > 0 ? (r.growth / v) * 100 : 0, 7)

/** A pool's mark, with its trailing space; `v`: what its quota counts now. */
export function poolMark(mode: PoolMark, r: Runway, pct: number, v: number, shape: Shape = 'circle'): string {
  if (mode === 'runway') return runwayMark(r, pct)
  if (mode === 'runway-hue') return runwayHue(r, pct, shape)
  const [d, n] = [weeklyDeg(r, v), hueStep(r, pct)]
  if (mode === 'arrow-circle') return `:arrow_deg${d}: :runway-${n}: `
  return `:pool-${mode === 'arrow' ? 'bare' : mode === 'arrow-disc' ? 'disc' : 'chip'}-${n}-${d}: `
}

export function runwayText(r: Runway, cfg: DigestCfg): string {
  if (!(r.days > 0 && r.days < RUNWAY_TEXT_D) || r.growth == null) return ''
  const days = r.days < 1 ? '<1d' : `${fixed(r.days, 0)}d`
  return `, +${fixed(r.growth, 1)} ${cfg.unit.short}/d, ≈${days} to full`
}

/** The pools in the reply order (`digest_cw._groups`): the primary's, then each other bucket's in the scan's meta
 *  order, a zone at its first member's position. */
function ordered(r: CwScan, cfg: DigestCfg): Pool[] {
  const all = pools(cfg)
  const out: Pool[] = []
  for (const b of [...(cfg.primary ? [cfg.primary] : []), ...Object.keys(r.extra)]) {
    for (const p of all) if (p.buckets.includes(b) && !out.includes(p)) out.push(p)
  }
  return out
}

export interface PoolRow { p: Pool; v: number; pct: number; mark: string; tail: string; text: string }

/** Every pool the scan `r` has, marked and with its headroom. `circles`: most full first, marked by share; `runway`:
 *  the replies' order, marked by runway over `seen` (every scan, in order); `runway-hue`: the same, as `:runway-N:`
 *  steps in each pool's shape. `text`: its share (below `QUIET_PCT`, whole percent alone) and runway. */
export function poolRows(r: CwScan, cfg: DigestCfg, alertAt: number, mode: 'circles' | PoolMark = 'circles', seen: CwScan[] = []): PoolRow[] {
  const u = cfg.unit
  const rows = (mode === 'circles' ? pools(cfg) : ordered(r, cfg)).flatMap(p => {
    const v = used(p, r, cfg)
    return v == null ? [] : [{ p, v, pct: pctOf(p, v, cfg) }]
  })
  if (mode === 'circles') rows.sort((a, b) => b.pct - a.pct)
  const t = scanTime(r.scan)
  const reach = t - (Math.max(...RUNWAY_WINDOWS_D) + 1) * 86400_000
  return rows.map(({ p, v, pct }) => {
    let m = mark(pct, alertAt)
    let tail = ''
    if (mode !== 'circles') {
      const hist = seen.filter(x => x.scan < r.scan && scanTime(x.scan) >= reach)
        .flatMap(x => { const y = used(p, x, cfg); return y == null ? [] : [[scanTime(x.scan), y] as [number, number]] })
      const rw = runway(p.quota.bytes / u.bytes - v, growth(hist, t, v))
      m = poolMark(mode, rw, pct, v, p.shape)
      tail = runwayText(rw, cfg)
    }
    if (pct < QUIET_PCT) return { p, v, pct, mark: m, tail, text: `${fixed(pct, 0)}%${tail}` }
    const free = p.quota.bytes / u.bytes - v
    const room = free >= 0 ? `${fixed(free, 1)} ${u.short} free` : `${fixed(-free, 1)} ${u.short} over`
    return { p, v, pct, mark: m, tail, text: `${fixed(pct, 1)}% of ${p.quota.short ?? p.quota.name} (${room})${tail}` }
  })
}

/** The OP's quota line at scan `r`: `poolRows`, one clause each. */
export function quotaLine(r: CwScan, cfg: DigestCfg, alertAt: number, mode: 'circles' | PoolMark = 'circles', seen: CwScan[] = []): string {
  const clauses = poolRows(r, cfg, alertAt, mode, seen).map(({ p, mark: m, text }) => `${m}${p.name} ${text}`)
  return clauses.length ? `*Quotas:* ${clauses.join(' · ')}` : ''
}
