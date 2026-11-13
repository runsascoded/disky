import { compactQuietLines, QUIET_BYTES } from './compact'
/** The `cw` digest template's replies (`digest_cw.day_rows` / `reply`, ported byte for byte; goldens:
 * `./replyParity.test.ts`), so `/admin/slack` can show a candidate's replies. The job still renders what it posts in
 * Python. `ArrowMark`s exist only here: a preview of marks with no emoji uploaded. Pure; DOM-free. */
import { scanTime } from '../scanSlug'
import { type DigestCfg, deg, pct, tb as signed, type Quota } from './common'
import { bucketLink, type CwRows, type CwScan, dextra, diffUrl, dlink, scoped } from './cw'
import { growth, hueStep, mark, poolMark, type PoolMark, QUIET_PCT, runway, type Runway, RUNWAY_WINDOWS_D, runwayText, weeklyDeg } from './pools'
import { dayOf, fixed, grouped, hhmm, md, pyRound } from './pyfmt'

export type ReplyMarks = 'warning' | 'circles' | PoolMark

/** What the job renders replies under, beyond the OP's config (`Cw.reply_json`). */
export interface ReplyCfg {
  quota_marks: ReplyMarks
  total_headline: boolean
  /** Absent: a bundle from before the option. */
  reply_lines?: boolean
  reply_hour: number
  provisional: boolean
  alert_pct: number | null
  op_sender: { icon_url?: string; icon_emoji?: string }
  avatar_rev: number
  /** Each scan's first run's trigger; a scan without one goes by the clock. */
  triggers: Record<string, string>
}

/** One quota pool's tile in a candidate's collage avatar: its `runway-hue` step and the arrow's angle (`poolMark`'s). */
export interface ReplyCell { n: number; deg: number }

export interface RenderedReply {
  key: string
  scan: string
  username: string
  body: string
  icon_url: string | null
  icon_emoji: string | null
  /** A candidate's collage avatar, in the reply's pool order; the job has no such avatar. */
  icon_cells?: ReplyCell[]
}

/** What a candidate overrides; absent: as the bundle was rendered. `lines`: each pool on its own line, its mark leading.
 *  `collage`: the sender avatar is one tile per pool, not the day's trend arrow. */
export interface ReplyOverrides { marks?: ReplyMarks; total?: boolean; lines?: boolean; collage?: boolean }

const HOURS_PER_WEEK = 168
const ALERT_MARK = ':warning:'
const LINK_ARROW = '↗︎'
const PROVISIONAL_ICON = ':hourglass_flowing_sand:'
const DAY_MS = 86400_000
const RUNWAY_MARKS: ReplyMarks[] = ['runway', 'runway-hue', 'arrow', 'arrow-disc', 'arrow-chip', 'arrow-circle']
const BRIEF_MARKS: ReplyMarks[] = ['circles', ...RUNWAY_MARKS]

interface DayRow {
  date: string
  s: CwScan
  dtb: number | null
  hours: number | null
  since: number | null
  dextra: Record<string, number | null>
  provisional: boolean
  hist: CwScan[]
}

const dateOf = (r: CwScan): string => r.scan.slice(0, 10)

function row(date: string, s: CwScan, prev: CwScan | null, seen: CwScan[], provisional = false): DayRow {
  const t = scanTime(s.scan)
  const reach = t - (Math.max(...RUNWAY_WINDOWS_D) + 1) * DAY_MS
  return {
    date, s, provisional,
    dtb: prev ? pyRound(s.tb - prev.tb, 1) : null,
    hours: prev ? (t - scanTime(prev.scan)) / 3600_000 : null,
    since: prev ? scanTime(prev.scan) : null,
    dextra: dextra(s, prev),
    hist: seen.filter(x => x.scan <= s.scan && scanTime(x.scan) >= reach),
  }
}

/** One row per in-month UTC day that has its reply scan (`day_rows`). */
function dayRows(m: CwRows, variant: string, rc: ReplyCfg, provisional = false): DayRow[] {
  const seen = [...m.pre, ...m.lead, ...m.rows]
  const days = new Map<string, CwScan[]>()
  for (const r of [...m.lead, ...m.rows]) days.set(dateOf(r), [...(days.get(dateOf(r)) ?? []), r])
  const replyable = (r: CwScan) => new Date(scanTime(r.scan)).getUTCHours() >= rc.reply_hour && rc.triggers[r.scan] !== 'manual'
  const first = dateOf(m.rows[0])
  const out: DayRow[] = []
  let prev: CwScan | null = null
  ;[...days.entries()].forEach(([date, rs], i) => {
    let s: CwScan | undefined
    if (variant === 'body') s = rs[rs.length - 1]
    else {
      const later = i + 1 < days.size || m.closed
      s = rs.find(replyable) ?? (later ? rs[rs.length - 1] : undefined)
      if (!s) {
        if (provisional && date >= first) out.push(row(date, rs[rs.length - 1], prev, seen, true))
        return
      }
    }
    if (date >= first) out.push(row(date, s, prev, seen))
    prev = s
  })
  return out
}

const qlabel = (b: number): string => (b >= 10 ** 15 ? `${b / 10 ** 15}P` : `${b / 10 ** 12}T`)

function quotaClause(v: number, q: Quota, cfg: DigestCfg, brief = false): string {
  const u = cfg.unit
  const qt = q.bytes / u.bytes
  const share = (v / qt) * 100
  if (brief && share < QUIET_PCT) return `${fixed(share, 0)}%`
  const room = v <= qt ? `${grouped(qt - v, 1)} ${u.short} free` : `${grouped(v - qt, 1)} ${u.short} over`
  return `${fixed(share, 1)}% of ${q.short || qlabel(q.bytes)} (${room})`
}

type Member = [string, number]

/** The day's buckets grouped by quota: a zone's at its first member's position (`_groups`). */
function groups(day: DayRow, cfg: DigestCfg): Member[][] {
  const out = new Map<string, Member[]>()
  for (const [b, v] of [[cfg.primary!, day.s.tb] as Member, ...Object.entries(day.s.extra)]) {
    const z = cfg.buckets[b]?.zone
    const key = z ? `zone:${z}` : `bucket:${b}`
    out.set(key, [...(out.get(key) ?? []), [b, v]])
  }
  return [...out.values()]
}

function fill(g: Member[], day: DayRow, cfg: DigestCfg): number | null {
  const u = cfg.unit.bytes
  if (g.length > 1) {
    const q = cfg.buckets[g[0][0]].quotas[0]
    return q ? (g.reduce((a, [, v]) => a + v, 0) / (q.bytes / u)) * 100 : null
  }
  const [name, v] = g[0]
  const b = cfg.buckets[name]
  if (!b || !b.quotas.length) return null
  const classes = name === cfg.primary ? day.s.classes : null
  return Math.max(...b.quotas.map(q => (scoped(v, classes, q) / (q.bytes / u)) * 100))
}

function groupUsed(g: string[], s: CwScan, q: Quota, cfg: DigestCfg): number {
  const size = (b: string) => (b === cfg.primary ? s.tb : s.extra[b] ?? 0)
  if (g.length > 1) return g.reduce((a, b) => a + size(b), 0)
  return g[0] === cfg.primary ? scoped(s.tb, s.classes, q) : size(g[0])
}

/** The group's shortest runway over its quotas (`_group_runway`), with what that quota counts at the day's scan. */
function groupRunway(g: string[], day: DayRow, cfg: DigestCfg): { r: Runway; v: number } | null {
  const qs = g.length > 1 ? cfg.buckets[g[0]].quotas.slice(0, 1) : cfg.buckets[g[0]]?.quotas ?? []
  if (!qs.length) return null
  const t = scanTime(day.s.scan)
  let best: { r: Runway; v: number } | null = null
  for (const q of qs) {
    const v = groupUsed(g, day.s, q, cfg)
    const hist = day.hist.filter(x => x.scan < day.s.scan).map(x => [scanTime(x.scan), groupUsed(g, x, q, cfg)] as [number, number])
    const r = runway(q.bytes / cfg.unit.bytes - v, growth(hist, t, v))
    if (!best || r.days < best.r.days) best = { r, v }
  }
  return best
}

function clause(g: Member[], day: DayRow, cfg: DigestCfg, marks: ReplyMarks): string {
  const brief = BRIEF_MARKS.includes(marks)
  const u = cfg.unit
  if (g.length === 1) {
    const [name, v] = g[0]
    const qs = cfg.buckets[name]?.quotas ?? []
    const link = bucketLink(name, day.s, day.since, cfg)
    if (!qs.length) return `${link}: ${grouped(v, 0)} ${u.short}`
    const classes = name === cfg.primary ? day.s.classes : null
    return `${link}: ${qs.map(q => quotaClause(scoped(v, classes, q), q, cfg, brief)).join(', ')}`
  }
  const q = cfg.buckets[g[0][0]].quotas[0]
  const parts = g.map(([b, v]) => `${bucketLink(b, day.s, day.since, cfg)} ${grouped(v, 0)}`).join(' + ')
  const total = g.reduce((a, [, v]) => a + v, 0)
  return `${parts} ${u.short}` + (q ? `: ${quotaClause(total, q, cfg, brief)}` : '')
}

/** The reply's per-bucket tail (`_tail`). */
function tail(day: DayRow, cfg: DigestCfg, rc: ReplyCfg, marks: ReplyMarks, sep: string): string {
  if (Object.keys(cfg.buckets).length <= 1 && !Object.keys(day.s.extra).length) {
    const b = cfg.buckets[cfg.primary!]
    const qs = b?.quotas ?? []
    const link = diffUrl(day.s.scan, dlink(day.s), day.since, cfg.site_url, !b || b.path === null ? cfg.primary! : b.path)
    const body = qs.length ? qs.map(q => quotaClause(scoped(day.s.tb, day.s.classes, q), q, cfg)).join(', ') : `${grouped(day.s.tb, 0)} ${cfg.unit.short}`
    return `${body} [${LINK_ARROW}](${link})`
  }
  const cs = groups(day, cfg).map(g => {
    const f = fill(g, day, cfg)
    const rw = RUNWAY_MARKS.includes(marks) ? groupRunway(g.map(([b]) => b), day, cfg) : null
    return { g, c: clause(g, day, cfg, marks), f, over: rc.alert_pct !== null && f !== null && f >= rc.alert_pct, rw }
  })
  if (marks === 'circles') cs.sort((a, b) => (b.f ?? -1) - (a.f ?? -1))
  const lines = cs.map(({ g, c, f, rw, over }) => {
    let text = c
    if (RUNWAY_MARKS.includes(marks) && f !== null && rw) {
      text = `${poolMark(marks as PoolMark, rw.r, f, rw.v, cfg.buckets[g[0][0]]?.shape ?? 'circle')}${c}${runwayText(rw.r, cfg)}`
    } else if (marks === 'circles' && f !== null) text = `${mark(f, rc.alert_pct ?? 90)}${c}`
    else if (!RUNWAY_MARKS.includes(marks) && marks !== 'circles' && over) text = `${ALERT_MARK} ${c}`
    const total = g.reduce((n, [, v]) => n + v, 0)
    const change = g.reduce((n, [b]) => n + (b === cfg.primary ? day.dtb ?? 0 : day.dextra[b] ?? 0), 0)
    return { text, quiet: total * cfg.unit.bytes < QUIET_BYTES && Math.abs(change) * cfg.unit.bytes < QUIET_BYTES && !over && (f === null || f < QUIET_PCT) && (!rw || rw.r.days >= 60) }
  })
  return (sep === '\n' ? compactQuietLines(lines) : lines.map(x => x.text)).join(sep)

}

/** The day's tile per pool that has a quota, in the reply's order. */
function cells(day: DayRow, cfg: DigestCfg): ReplyCell[] {
  return groups(day, cfg).flatMap(g => {
    const [f, rw] = [fill(g, day, cfg), groupRunway(g.map(([b]) => b), day, cfg)]
    return f === null || !rw ? [] : [{ n: hueStep(rw.r, f), deg: weeklyDeg(rw.r, rw.v) }]
  })
}

function headline(day: DayRow, total: boolean): [number, number] {
  if (!total) return [day.s.tb, day.dtb ?? 0]
  const sum = (xs: (number | null)[]) => xs.reduce<number>((a, x) => (x === null ? a : a + x), 0)
  return [pyRound(day.s.tb + sum(Object.values(day.s.extra)), 1), pyRound((day.dtb ?? 0) + sum(Object.values(day.dextra)), 1)]
}

function reply(day: DayRow, variant: string, cfg: DigestCfg, rc: ReplyCfg, marks: ReplyMarks, total: boolean, lines: boolean, collage: boolean): RenderedReply {
  const [v, dv] = headline(day, total)
  const prev = v - dv
  const d = deg(prev ? (dv / prev) * 100 : 0, day.hours ? HOURS_PER_WEEK / day.hours : 7)
  const size = `${grouped(v, 0)} ${cfg.unit.name} (${signed(dv)}, ${pct(dv, v)}%)`
  const sep = lines ? '\n' : ' · '
  const t = tail(day, cfg, rc, marks, sep)
  const when = md(dayOf(day.date))
  const url = diffUrl(day.s.scan, dlink(day.s), day.since, cfg.site_url)
  const base = { key: day.date, scan: day.s.scan }
  if (day.provisional) {
    return { ...base, username: `${when} · so far`, body: `:arrow_deg${d}: **${size}** · [as of ${hhmm(scanTime(day.s.scan))}Z](${url})${sep}${t}`, icon_url: null, icon_emoji: PROVISIONAL_ICON }
  }
  if (variant === 'sender') {
    const sender = { ...base, username: `${when} — ${size}`, body: t, icon_url: `${cfg.icons_base}/arrows/av_deg${d}.png?v=${rc.avatar_rev}`, icon_emoji: null }
    return collage ? { ...sender, icon_url: null, icon_cells: cells(day, cfg) } : sender
  }
  return { ...base, username: cfg.title, body: `:arrow_deg${d}: [${when}](${url}) — **${size}**${sep}${t}`, icon_url: rc.op_sender.icon_url ?? null, icon_emoji: rc.op_sender.icon_emoji ?? null }
}

/** The month's replies as the converge posts them, newest first (`digest.replies_json`). */
export function replies(rows: CwRows, cfg: DigestCfg, rc: ReplyCfg, variant: string, over: ReplyOverrides = {}): RenderedReply[] {
  const marks = over.marks ?? rc.quota_marks
  const total = over.total ?? rc.total_headline
  const days = dayRows(rows, variant, rc)
  if (rc.provisional && variant === 'sender') {
    const open = dayRows(rows, variant, rc, true).at(-1)
    if (open?.provisional) days.push(open)
  }
  return days.map(d => reply(d, variant, cfg, rc, marks, total, over.lines ?? rc.reply_lines ?? false, over.collage ?? false)).reverse()
}
