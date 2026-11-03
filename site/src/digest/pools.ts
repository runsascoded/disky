/** Quota pools for the `cw` template's quota-pressure variants (specs/digest-quota-pressure.md): each quota's used
 * share per scan, buckets sharing a `zone` summed against the zone's quota (as `digest_cw._groups`), sorted by
 * pressure; and the OP's quota line. Pure; DOM-free. */
import type { CwScan } from './cw'
import type { DigestCfg, Quota } from './common'
import { scanTime } from '../scanSlug'
import { fixed } from './pyfmt'

export interface Pool {
  /** The bucket's label, or the zone's name for buckets sharing one. */
  name: string
  /** The member buckets (config order). */
  buckets: string[]
  quota: Quota
}

/** The config's pools: one per quota of each bucket without a zone, one per zone (its first member's quotas). */
export function pools(cfg: DigestCfg): Pool[] {
  const out: Pool[] = []
  const zones = new Map<string, string[]>()
  for (const [b, c] of Object.entries(cfg.buckets)) {
    if (!c.quotas.length) continue
    if (c.zone) zones.set(c.zone, [...(zones.get(c.zone) ?? []), b])
    else for (const q of c.quotas) out.push({ name: c.quotas.length > 1 ? `${c.label || b} (${q.short ?? q.name})` : c.label || b, buckets: [b], quota: q })
  }
  for (const [z, bs] of zones) for (const q of cfg.buckets[bs[0]].quotas) out.push({ name: bs.length > 1 ? `${z} zone` : cfg.buckets[bs[0]].label || bs[0], buckets: bs, quota: q })
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

/** The OP's quota line at scan `r`: every pool the scan has, each marked and with its headroom. `circles`: most full
 *  first, marked by share; `runway`: the replies' order, marked by runway over `seen` (every scan, in order). */
export function quotaLine(r: CwScan, cfg: DigestCfg, alertAt: number, mode: 'circles' | 'runway' = 'circles', seen: CwScan[] = []): string {
  const u = cfg.unit
  const rows = (mode === 'runway' ? ordered(r, cfg) : pools(cfg)).flatMap(p => {
    const v = used(p, r, cfg)
    return v == null ? [] : [{ p, v, pct: pctOf(p, v, cfg) }]
  })
  if (mode === 'circles') rows.sort((a, b) => b.pct - a.pct)
  const t = scanTime(r.scan)
  const reach = t - (Math.max(...RUNWAY_WINDOWS_D) + 1) * 86400_000
  const clauses = rows.map(({ p, v, pct }) => {
    let m = mark(pct, alertAt)
    let tail = ''
    if (mode === 'runway') {
      const hist = seen.filter(x => x.scan < r.scan && scanTime(x.scan) >= reach)
        .flatMap(x => { const y = used(p, x, cfg); return y == null ? [] : [[scanTime(x.scan), y] as [number, number]] })
      const rw = runway(p.quota.bytes / u.bytes - v, growth(hist, t, v))
      m = runwayMark(rw, pct)
      tail = runwayText(rw, cfg)
    }
    if (pct < QUIET_PCT) return `${m}${p.name} ${fixed(pct, 0)}%${tail}`
    const free = p.quota.bytes / u.bytes - v
    const room = free >= 0 ? `${fixed(free, 1)} ${u.short} free` : `${fixed(-free, 1)} ${u.short} over`
    return `${m}${p.name} ${fixed(pct, 1)}% of ${p.quota.short ?? p.quota.name} (${room})${tail}`
  })
  return clauses.length ? `*Quotas:* ${clauses.join(' · ')}` : ''
}
