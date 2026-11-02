/** Quota pools for the `cw` template's quota-pressure variants (specs/digest-quota-pressure.md): each quota's used
 * share per scan, buckets sharing a `zone` summed against the zone's quota (as `digest_cw._groups`), sorted by
 * pressure; and the OP's quota line. Pure; DOM-free. */
import type { CwScan } from './cw'
import type { DigestCfg, Quota } from './common'
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

/** The pressure mark for a share: red at or over `alertAt` (or the quota), orange within 10 points of it. */
export function mark(pct: number, alertAt: number): string {
  if (pct >= Math.min(alertAt, 100)) return ':red_circle: '
  if (pct >= alertAt - 10) return ':large_orange_circle: '
  return ''
}

/** Below this share, a pool's clause is just its name and whole percent. */
export const QUIET_PCT = 25

/** The OP's quota line at scan `r`: every pool the scan has, most full first, each marked and with its headroom. */
export function quotaLine(r: CwScan, cfg: DigestCfg, alertAt: number): string {
  const u = cfg.unit
  const rows = pools(cfg).flatMap(p => {
    const v = used(p, r, cfg)
    return v == null ? [] : [{ p, v, pct: pctOf(p, v, cfg) }]
  }).sort((a, b) => b.pct - a.pct)
  const clauses = rows.map(({ p, v, pct }) => {
    if (pct < QUIET_PCT) return `${p.name} ${fixed(pct, 0)}%`
    const free = p.quota.bytes / u.bytes - v
    const room = free >= 0 ? `${fixed(free, 1)} ${u.short} free` : `${fixed(-free, 1)} ${u.short} over`
    return `${mark(pct, alertAt)}${p.name} ${fixed(pct, 1)}% of ${p.quota.short ?? p.quota.name} (${room})`
  })
  return clauses.length ? `*Quotas:* ${clauses.join(' · ')}` : ''
}
