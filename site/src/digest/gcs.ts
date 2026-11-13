/** The `gcs` digest template's OP: its text and its storage-class plot's `/api/digest-image` request, from the
 * month's per-day rows (the job sends them: `Gcs.rows_json`; goldens: `./parity.test.ts`). Pure; DOM-free. */
import { encodeScan, scanTime } from '../scanSlug'
import { deg, type DigestCfg, type DigestParams, type OpLinks, pct, prevMonthLink, span, tb, usd, weekOf, windowLabel, windowStart } from './common'
import { addDays, dayOf, grouped, iso, md, mon3, monthYear, pyRound } from './pyfmt'

/** One day's sender scan: TiB total and per class, $/mo, deltas vs the previous day's (null: none). */
export interface GcsRow {
  /** The scan id (`YYYY-MM-DD[THHMM]`). */
  date: string
  slug: string | null
  tb: number
  cost: number
  dtb: number | null
  dcost: number | null
  std: number
  near: number
  cold: number
  arch: number
  /** The previous day's scan id (the deltas' baseline); null only with no prior scan. */
  prev: string | null
}

/** A month's rows (`rows`, each a reply) and the OP window's days before it (`pre`). */
export interface GcsRows { template: 'gcs'; pre: GcsRow[]; rows: GcsRow[] }

const TIB = 1024 ** 4
// the image's tiers, bottom → top (cold → hot)
const TIERS = [['arch', 'Archive'], ['cold', 'Coldline'], ['near', 'Nearline'], ['std', 'Standard']] as const

const day = (r: GcsRow): number => dayOf(r.date)
const dlink = (r: GcsRow): string => r.slug ?? encodeScan(r.date)!

/** The month's window: its first day, every row held, its baseline `[TiB, scan id]` (`Month.window` / `Month.back`). */
function window(m: GcsRows, month: number, p: DigestParams): { start: number; seen: GcsRow[]; back: [number, string] } {
  const start = windowStart(month, day(m.rows[m.rows.length - 1]), p.windowDays)
  const seen = [...m.pre, ...m.rows]
  const before = seen.filter(r => day(r) < start)
  const back: [number, string] = before.length
    ? [before[before.length - 1].tb, before[before.length - 1].date]
    : [seen[0].tb - (seen[0].dtb ?? 0), seen[0].prev ?? seen[0].date]
  return { start, seen, back }
}

/** The OP's markdown (`digest_gcs.op_body`). */
export function opText(m: GcsRows, month: number, cfg: DigestCfg, p: DigestParams, links: OpLinks): string {
  const site = cfg.site_url
  const rows = m.rows
  const baseTb = rows[0].tb - (rows[0].dtb ?? 0)
  const mdtb = rows[rows.length - 1].tb - baseTb
  const mweekly = baseTb ? ((((mdtb / baseTb) * 100) * 7) / rows.length) : 0
  const { start, seen, back: [backTb, backAt] } = window(m, month, p)
  const last = rows[rows.length - 1]
  const backUrl = `${site}/?d=${dlink(last)}-${span(scanTime(backAt), scanTime(last.date))}#over-time`
  const figures: Record<'mtd' | 'window', string> = {
    mtd: `**${tb(mdtb)} TiB** month-to-date · `,
    // (none when it starts where month-to-date does: a store whose first scan is this month's)
    window: backAt !== (rows[0].prev ?? rows[0].date) ? `**${tb(last.tb - backTb)} TiB** [${windowLabel(start, backAt, p.windowDays)}](${backUrl}) · ` : '',
  }
  const lines = [`:arrow_deg${deg(mweekly)}: ${p.headline.map(f => figures[f]).join('')}[dashboard](${site}/)`, '', '*Weekly summaries*']
  const mon0 = weekOf(p.weeks === 'month' ? month : start)
  const weeks = new Map<number, GcsRow[]>()
  for (const r of seen) {
    if (day(r) < mon0) continue
    const k = weekOf(day(r))
    weeks.set(k, [...(weeks.get(k) ?? []), r])
  }
  const lastMon = [...weeks.keys()].pop()
  const first = weeks.values().next().value![0]
  // the first week's baseline: its first row's delta baseline; absent one, a day before that row
  const base0 = first.prev ? scanTime(first.prev) : addDays(scanTime(first.date), -1)
  const [wTb, wCost] = [first.tb - (first.dtb ?? 0), first.cost - (first.dcost ?? 0)]
  let prevEnd: GcsRow | null = null
  for (const [mon, ws] of weeks) {
    const end = ws[ws.length - 1]
    if (end === first && first.dtb === null) continue
    const [bTb, bCost] = prevEnd !== null ? [prevEnd.tb, prevEnd.cost] : [wTb, wCost]
    const bAt = prevEnd !== null ? scanTime(prevEnd.date) : base0
    const wdtb = end.tb - bTb
    const wpct = bTb ? (wdtb / bTb) * 100 : 0
    const partial = new Set(ws.map(day)).size < 7 && mon === lastMon ? ' _(partial)_' : ''
    const earlier = addDays(mon, 6) < month ? ` (${mon3(day(end))})` : ''
    lines.push(
      `:arrow_deg${deg(wpct)}: [wk of ${md(mon)}](${site}/?d=${dlink(end)}-${span(bAt, scanTime(end.date))}#over-time)${earlier}${partial} — ` +
        `**${grouped(end.tb, 0)} TiB** (${tb(wdtb)}, ${pct(wdtb, end.tb)}%) · $${grouped(end.cost, 0)}/mo (${usd(end.cost - bCost)})`,
    )
    prevEnd = end
  }
  if (links.prevUrl) lines.push('', ...prevMonthLink(month, links.prevUrl))
  if (links.plotUrl !== null) lines.push('', `![${cfg.title} — ${monthYear(month)}](${links.plotUrl})`)
  return lines.join('\n')
}

/** The plot's `/api/digest-image` request (`digest_gcs.image_body`, `kind: tiers`). */
export function imageRequest(m: GcsRows, month: number, cfg: DigestCfg, p: DigestParams): Record<string, unknown> {
  const rows = m.rows
  const { start, seen, back } = window(m, month, p)
  const before = seen.filter(r => day(r) < start)
  const byDay = new Map<string, GcsRow>()
  for (const r of [...before.slice(-1), ...seen.filter(r => day(r) >= start)]) byDay.set(r.date.slice(0, 10), r)
  const body: Record<string, unknown> = {
    kind: 'tiers',
    title: `${cfg.title} — ${monthYear(month)}`,
    note: `${cfg.host} · TiB`,
    month: iso(month).slice(0, 7),
    from: iso(start),
    units: 'TiB',
    tiers: TIERS.map(([, name]) => name),
    points: [...byDay.values()].map(r => ({ scan: r.date, bytes: TIERS.map(([k]) => pyRound(r[k] * TIB)) })),
  }
  const from = p.diff === 'mtd' ? rows[0].prev ?? rows[0].date : back[1]
  const to = rows[rows.length - 1].date
  if (from < to) {
    const [a, z] = [scanTime(from), scanTime(to)]
    body.diff = { from, to, path: '', title: `What changed — ${md(a)} → ${md(z)}` }
  }
  return body
}
