/** The `cw` digest template's OP: its text and its plot's `/api/digest-image` request, from the month's scans
 * (the job sends them: `Cw.rows_json`; goldens: `./parity.test.ts`). The replies stay the job's. Pure; DOM-free. */
import { encodeScan, scanTime } from '../scanSlug'
import { type BucketCfg, deg, type DigestCfg, type DigestParams, type OpLinks, prevMonthLink, type Quota, span, tb, weekOf, windowLabel, windowStart } from './common'
import { pools, quotaLine, used } from './pools'
import { addDays, dayOf, fixed, grouped, hhmm, iso, md, mon3, monthYear, pyRound } from './pyfmt'

export interface CwScan {
  scan: string
  /** Its canonical `?d=` slug; null: its minute. */
  slug: string | null
  /** The primary's size, in the config's unit (rounded to 0.1). */
  tb: number
  /** The other buckets' sizes, in meta order. */
  extra: Record<string, number>
  /** The primary's per-storage-class sizes (unrounded); null: the meta has none. */
  classes: Record<string, number> | null
}

/** A month's scans: the OP window's before the lead-in day (`pre`), the lead-in day's (`lead`), the month's (`rows`). */
export interface CwRows { template: 'cw'; closed: boolean; pre: CwScan[]; lead: CwScan[]; rows: CwScan[] }

const date = (r: CwScan): string => r.scan.slice(0, 10)
const ts = (r: CwScan): number => scanTime(r.scan)
const dlink = (r: CwScan): string => r.slug ?? encodeScan(r.scan)!

/** What quota `q` counts of a scan (`_scoped`): its classes' sum, or the total for a quota over every class. */
function scoped(total: number, classes: Record<string, number> | null, q: Quota): number {
  if (!q.classes.length) return total
  if (!classes) throw new Error(`quota ${q.name} counts storage classes ${q.classes}, but the scan's meta has no class_bytes`)
  let s = 0
  for (const c of q.classes) s += classes[String(c)] ?? 0
  return s
}

const quotas = (total: number, classes: Record<string, number> | null, qs: Quota[], cfg: DigestCfg): string =>
  qs.map(q => ` · ${fixed((scoped(total, classes, q) / (q.bytes / cfg.unit.bytes)) * 100, 1)}% of ${q.name}`).join('')

function diffUrl(scan: string, slug: string, since: number | null, site: string, bucket = ''): string {
  const s = since !== null ? `-${span(since, scanTime(scan))}` : ''
  return `${site}/${bucket}?d=${slug}${s}#over-time`
}

function bucketLink(bucket: string, r: CwScan, since: number | null, cfg: DigestCfg): string {
  const b: BucketCfg | undefined = cfg.buckets[bucket]
  const path = b === undefined || b.path === null ? bucket : b.path
  return `[${b?.label || bucket}](${diffUrl(r.scan, dlink(r), since, cfg.site_url, path)})`
}

function dextra(cur: CwScan, prev: CwScan | null): Record<string, number | null> {
  return Object.fromEntries(Object.entries(cur.extra).map(([b, t]) => [b, prev && b in prev.extra ? pyRound(t - prev.extra[b], 1) : null]))
}

function extras(r: CwScan, d: Record<string, number | null>, since: number | null, cfg: DigestCfg): string {
  return Object.entries(r.extra).map(([b, t]) => ` · ${bucketLink(b, r, since, cfg)} ${grouped(t, 0)} ${cfg.unit.name}` + (d[b] != null ? ` (${tb(d[b]!)})` : '')).join('')
}

/** The month's window: its first day, every scan held, its baseline (`Month.window` / `Month.back`). */
function window(m: CwRows, month: number, p: DigestParams): { start: number; seen: CwScan[]; back: CwScan } {
  const start = windowStart(month, dayOf(date(m.rows[m.rows.length - 1])), p.windowDays)
  const seen = [...m.pre, ...m.lead, ...m.rows]
  const before = seen.filter(r => date(r) < iso(start))
  return { start, seen, back: before.length ? before[before.length - 1] : seen[0] }
}

const base = (m: CwRows): CwScan => (m.lead.length ? m.lead[m.lead.length - 1] : m.rows[0])
const primaryQuotas = (cfg: DigestCfg): Quota[] => (cfg.primary ? cfg.buckets[cfg.primary]?.quotas ?? [] : [])

/** The OP's markdown (`digest_cw.op_body`). */
export function opText(m: CwRows, month: number, cfg: DigestCfg, p: DigestParams, links: OpLinks): string {
  const site = cfg.site_url
  const u = cfg.unit
  const qs = primaryQuotas(cfg)
  const rows = m.rows
  const b0 = base(m)
  const last = rows[rows.length - 1]
  const mdtb = last.tb - b0.tb
  const days = (ts(last) - ts(b0)) / 86400_000 || 1
  const mweekly = b0.tb ? ((((mdtb / b0.tb) * 100) * 7) / days) : 0
  const mtdSince = b0 !== last ? ts(b0) : null
  const mtdUrl = diffUrl(last.scan, dlink(last), mtdSince, site)
  const primary = cfg.primary && Object.keys(last.extra).length ? `${bucketLink(cfg.primary, last, mtdSince, cfg)} ` : ''
  const { start, seen, back } = window(m, month, p)
  const backUrl = diffUrl(last.scan, dlink(last), back !== last ? ts(back) : null, site)
  const figures: Record<'mtd' | 'window', string> = {
    mtd: `**${tb(mdtb)} ${u.name}** [month-to-date](${mtdUrl}) · `,
    // (none when it starts where month-to-date does: a store whose first scan is this month's)
    window: back !== b0 ? `**${tb(last.tb - back.tb)} ${u.name}** [${windowLabel(start, back.scan, p.windowDays)}](${backUrl}) · ` : '',
  }
  const lines = [
    `:arrow_deg${deg(mweekly)}: ${p.headline.map(f => figures[f]).join('')}${primary}${grouped(last.tb, 0)} ${u.name}${quotas(last.tb, last.classes, qs, cfg)}` +
      `${extras(last, dextra(last, b0 !== last ? b0 : null), mtdSince, cfg)} · [dashboard](${site}/)`,
    ...(p.quotas === 'off' ? [] : [quotaLine(last, cfg, p.alertAt, p.quotas, seen)].filter(Boolean)),
    `_as of ${md(dayOf(date(last)))} ${hhmm(ts(last))}Z_`,
    '',
    '*Weekly summaries*',
  ]
  const mon0 = iso(weekOf(p.weeks === 'month' ? month : start))
  const older = seen.filter(r => date(r) < mon0)
  const weeks = new Map<number, CwScan[]>()
  for (const r of seen) {
    if (date(r) < mon0) continue
    const k = weekOf(dayOf(date(r)))
    weeks.set(k, [...(weeks.get(k) ?? []), r])
  }
  const lastMon = [...weeks.keys()].pop()
  let prevEnd = older.length ? older[older.length - 1] : seen[0]
  for (const [mon, ws] of weeks) {
    const end = ws[ws.length - 1]
    if (end === prevEnd) continue
    const wdtb = end.tb - prevEnd.tb
    const wpct = prevEnd.tb ? (wdtb / prevEnd.tb) * 100 : 0
    const partial = mon === lastMon && dayOf(date(end)) < addDays(mon, 6) ? ' _(partial)_' : ''
    const earlier = addDays(mon, 6) < month ? ` (${mon3(dayOf(date(end)))})` : ''
    const link = diffUrl(end.scan, dlink(end), prevEnd !== end ? ts(prevEnd) : null, site)
    lines.push(
      `:arrow_deg${deg(wpct)}: [wk of ${md(mon)}](${link})${earlier}${partial}: ` +
        `**${tb(wdtb)} ${u.name}** → ${grouped(end.tb, 0)} ${u.name}${quotas(end.tb, end.classes, qs, cfg)}`,
    )
    prevEnd = end
  }
  if (links.prevUrl) lines.push('', ...prevMonthLink(month, links.prevUrl))
  if (links.plotUrl !== null) lines.push('', `![${cfg.title} — ${monthYear(month)}](${links.plotUrl})`)
  return lines.join('\n')
}

/** The plot's `/api/digest-image` request (`digest_cw.image_body`). */
export function imageRequest(m: CwRows, month: number, cfg: DigestCfg, p: DigestParams): Record<string, unknown> {
  const u = cfg.unit
  const last = m.rows[m.rows.length - 1]
  const { start, seen, back } = window(m, month, p)
  const scans = [back, ...seen.filter(r => date(r) >= iso(start) && r !== back)]
  const q = primaryQuotas(cfg)[0]
  const body: Record<string, unknown> = {
    title: `${cfg.title} — ${monthYear(month)}`,
    note: [cfg.host, ...(cfg.primary && Object.keys(last.extra).length ? [cfg.primary] : []), u.name].join(' · '),
    month: iso(month).slice(0, 7),
    from: iso(start),
    points: scans.map(r => ({ scan: r.scan, bytes: pyRound((q ? scoped(r.tb, r.classes, q) : r.tb) * u.bytes) })),
    units: u.name,
  }
  if (q) body.quota = { bytes: q.bytes, name: q.name }
  if (p.plot === 'pools') {
    // every pool's share of its quota over the same scans (a scan without the pool's buckets: no point)
    body.kind = 'pools'
    body.alert_pct = p.alertAt
    body.pools = pools(cfg).map(pl => ({
      name: pl.name,
      quota: pl.quota.bytes,
      points: scans.flatMap(r => {
        const v = used(pl, r, cfg)
        return v == null ? [] : [{ scan: r.scan, bytes: pyRound(v * u.bytes) }]
      }),
    })).filter(pl => pl.points.length)
  }
  const from = p.diff === 'mtd' ? base(m) : back
  if (from !== last) {
    const s = `${md(dayOf(date(from)))} → ${md(dayOf(date(last)))}`
    body.diff = { from: from.scan, to: last.scan, path: cfg.primary ?? '', title: cfg.primary ? `What changed in ${cfg.primary} — ${s}` : `What changed — ${s}` }
  }
  return body
}
