/**
 * The monthly admin thread's opening post (specs/admin-monthly-thread.md): a deployment's scan runs over a UTC month as
 * the few lines its `#<deploy>-admin` thread opens with, under the status card. The site renders it
 * (`POST /api/scan-runs/month-op`) for the scan job (`dt_cloud.nominal`) to post; the fleet app can render each
 * deployment's reply with it too. Pure and DOM-free: its inputs are the store's runs (`/api/scan-runs`' summaries), the
 * meta store's sizes and the month.
 *
 *   ✅ 12 consecutive clean runs · latest [11/24 07:17Z](…) · 46m 40s · $0.17 · output 1.20 G (+12.0 M)
 *   This month: 41 runs · 38 clean · 2 with issues · 1 failed · 12 scheduled / 29 manual
 *   Cost: $6.31 month-to-date · $7.02 last 30d · run time 43m (7d median, +19%)
 *   [Metadata](…): 49.9 G · +3.06 G over 30d · [All scans](…)
 *   [← October](…) · [December →](…)
 *
 * A run's outcome is `nominal`'s (`grade`), so the status line says what the run thread's head says today: `clean` is a
 * run that succeeded with no phase issue but a deferred append, whether or not a later run recovered it (the card's
 * `runOutcome` counts only issues still open). Figures and formats mirror `nominal` (`stamp`, `took`, `outcome`, `si`);
 * the run time is the card's (`stripModel`). A closed month (`now` past its end) is drawn as of its last second: no
 * status line, the month's final counts and cost, the windows ending with it.
 */
import { stock } from '../emoji.js'
import { cardRuns, type RunIn, SCAN_KIND, stripModel } from '../og/deployCard.js'

const DAY = 86400

/** A run's outcome as `nominal` grades it (`grade`), and a live run's. */
export type Grade = 'clean' | 'issues' | 'failed' | 'cancelled' | 'running'
/** The month line's order. */
export const GRADES: readonly Grade[] = ['clean', 'issues', 'failed', 'cancelled', 'running']
const WORDS: Record<Grade, string> = { clean: 'clean', issues: 'with issues', failed: 'failed', cancelled: 'cancelled', running: 'running' }

/** Phase issues that leave a run clean (`nominal.TOLERATED`, the site's `DEFERRED`). */
export const TOLERATED = ['deferred']

/** A run as `/api/scan-runs` serves it (`RunSummary`): the fields the post reads. */
export interface OpRun extends RunIn {
  run: RunIn['run'] & { run_id: string; parent: string | null; finished_ts: number | null; failed_phase: string | null }
  out_bytes: number | null
  delta_bytes: number | null
  phase_issues: { phase: string; outcome: string; recovery: unknown; passing?: unknown }[]
}

/** A store's size at a scan (epoch s). */
export interface SizePoint { t: number; bytes: number }
/** The meta store's series: its points (any order), or the HTTP status its read failed with. */
export type MetaSeries = { points: SizePoint[] } | { status: number }

export interface MonthOpInput {
  /** The store's runs, any order, any kind (only its own scan runs count: `SCAN_KIND`, no parent, no `nop`). */
  runs: readonly OpRun[]
  /** `YYYY-MM`, UTC. */
  month: string
  /** Epoch s; on or after the month's start. */
  now: number
  /** The meta store (`STORES_JSON` `meta`) and its root series; null: the deployment has none (no Metadata figures). */
  meta: { store: string; series: MetaSeries } | null
}

export interface MonthOpCfg {
  /** The deployment's site (`https://…`): run records and pages link under it. */
  site: string
  /** The runs' store (default `primary`): its `/scans` page. */
  store?: string
  /** The previous and next months' threads (permalinks). */
  prevUrl?: string | null
  nextUrl?: string | null
  /** Outcome emoji over the vocabulary's stock ones (`nominal.Config.emoji`). */
  marks?: Partial<Record<Grade, string>>
}

export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
const MONTH_RE = /^(\d{4})-(0[1-9]|1[0-2])$/

/** A month's bounds (epoch s): its first second, and the next month's. */
export function monthBounds(month: string): { start: number; end: number; index: number } {
  const m = MONTH_RE.exec(month)
  if (!m) throw new Error(`month: YYYY-MM, not ${JSON.stringify(month)}`)
  const [y, i] = [+m[1], +m[2] - 1]
  return { start: Date.UTC(y, i, 1) / 1000, end: Date.UTC(y, i + 1, 1) / 1000, index: i }
}

// ── nominal's figures ─────────────────────────────────────────────────────

const live = (r: OpRun) => r.run.status === 'running' || r.run.finished_ts == null
const issues = (r: OpRun) => r.phase_issues.filter(i => !TOLERATED.includes(i.outcome))
const succeeded = (r: OpRun) => r.run.status === 'succeeded' && r.run.finished_ts != null

/** A run's outcome (`nominal.grade`, a live run `running`). */
export function grade(r: OpRun): Grade {
  if (live(r)) return 'running'
  if (succeeded(r) && !issues(r).length) return 'clean'
  if (r.run.status === 'cancelled') return 'cancelled'
  return succeeded(r) ? 'issues' : 'failed'
}

/** An unclean run's outcome in a few words (`nominal.short`). */
export function short(r: OpRun): string {
  if (r.run.status === 'failed') return 'failed' + (r.run.failed_phase ? ` in ${r.run.failed_phase}` : '')
  if (r.run.status === 'cancelled') return 'cancelled'
  const phases = [...new Set(issues(r).map(i => i.phase))].sort()
  return phases.length ? `phase issues: ${phases.join(', ')}` : r.run.status
}

const pad = (n: number) => String(n).padStart(2, '0')
/** A run's start, `11/24 07:17Z` (`nominal.stamp`). */
export function stamp(t: number): string {
  const d = new Date(t * 1000)
  return `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`
}

/** A duration (`nominal.hm`): under an hour `46m 40s`, from an hour `2h 3m` (to the nearest minute, a half up). */
export function took(secs: number): string {
  if (secs < 3600) return `${Math.trunc(secs / 60)}m ${pad(Math.trunc(secs) % 60)}s`
  const m = Math.floor(secs / 60 + 0.5)
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** Bytes at three significant figures in SI units (`289 T`, `46.8 G`, `440 M`); `sign`: `+`/`−` first (`nominal.si`). */
export function si(n: number, sign = false): string {
  const head = sign ? (n >= 0 ? '+' : '−') : n < 0 ? '-' : ''
  const a = Math.abs(n)
  for (const [div, unit] of [[1e15, 'P'], [1e12, 'T'], [1e9, 'G'], [1e6, 'M'], [1e3, 'K']] as const) {
    if (a >= div) {
      const v = a / div
      return `${head}${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${unit}`
    }
  }
  return `${head}${Math.trunc(a)} B`
}

export const usd = (x: number): string => `$${x.toFixed(2)}`

/** A finished run's duration, cost and output, as far as recorded (`nominal.outcome`). */
export function outcome(r: OpRun): string {
  const parts = r.secs == null ? [] : [took(r.secs)]
  if (r.cost_usd != null) parts.push(usd(r.cost_usd))
  if (r.out_bytes != null) parts.push(`output ${si(r.out_bytes)}` + (r.delta_bytes == null ? '' : ` (${si(r.delta_bytes, true)})`))
  return parts.join(' · ')
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

// ── the post ──────────────────────────────────────────────────────────────

/** The store's `/scans` page (`nominal.Config.scans`). */
export const scansUrl = (cfg: MonthOpCfg): string =>
  cfg.site.replace(/\/+$/, '') + (cfg.store && cfg.store !== 'primary' ? `/${encodeURIComponent(cfg.store)}` : '') + '/scans'
/** A run's record (`nominal.Config.link`). */
export const runUrl = (cfg: MonthOpCfg, r: OpRun): string => `${scansUrl(cfg)}/${encodeURIComponent(r.run.run_id)}`
/** A store's page (`nominal.Config.page`). */
export const pageUrl = (cfg: MonthOpCfg, store: string): string =>
  cfg.site.replace(/\/+$/, '') + (store === 'primary' ? '/' : `/${encodeURIComponent(store)}`)

const mark = (cfg: MonthOpCfg, g: Grade) => cfg.marks?.[g] ?? stock(`run.${g}`)

/** The status line: the newest finished run's outcome and the streak it ends (consecutive clean runs, or consecutive
 *  unclean ones), with its time, an unclean run's reason, and its duration, cost and output. `rows` newest first. */
export function statusLine(rows: readonly OpRun[], cfg: MonthOpCfg): string {
  const done = rows.filter(r => !live(r) && r.run.status !== 'cancelled')
  if (!done.length) return 'No finished runs yet'
  const clean = grade(done[0]) === 'clean'
  const end = done.findIndex(r => (grade(r) === 'clean') !== clean)
  const run = end < 0 ? done : done.slice(0, end)
  const qualifier = end < 0 ? 'at least ' : ''
  const n = run.length, s = n === 1 ? '' : 's'
  let head: string
  if (clean) head = `${mark(cfg, 'clean')} ${qualifier}${n} consecutive clean run${s}`
  else if (run.every(r => grade(r) === 'issues')) head = `${mark(cfg, 'issues')} ${qualifier}${n} consecutive run${s} with phase issues`
  else {
    const mixed = run.some(r => grade(r) === 'issues')
    head = `${mark(cfg, 'failed')} ${qualifier}${n} consecutive ` + (mixed ? `run${s} failed or with phase issues` : `failed run${s}`)
  }
  const latest = done[0]
  const tail = [clean ? '' : short(latest), outcome(latest)].filter(Boolean)
  return [head, `latest [${stamp(latest.run.started_ts!)}](${runUrl(cfg, latest)})`, ...tail].join(' · ')
}

/** The month's counts: its runs by outcome (`GRADES`' order, none of an outcome left out) and by trigger. */
export function monthLine(rows: readonly OpRun[], label: string, closed: boolean): string {
  if (!rows.length) return `${label}: no runs${closed ? '' : ' yet'}`
  const by = GRADES.map(g => [g, rows.filter(r => grade(r) === g).length] as const).filter(([, n]) => n)
  const trig = (t: string | null) => rows.filter(r => (r.run.trigger ?? null) === t).length
  const unknown = trig(null)
  const mix = `${trig('schedule')} scheduled / ${trig('manual')} manual` + (unknown ? ` / ${unknown} unknown` : '')
  return [`${label}: ${plural(rows.length, 'run')}`, ...by.map(([g, n]) => `${n} ${WORDS[g]}`), mix].join(' · ')
}

/** The cost line: the month's recorded cost and the last 30 days', and the card's run time (`stripModel`: the median
 *  of the last 7 days' succeeded runs, and its change against the 7 before). */
export function costLine(rows: readonly OpRun[], inMonth: readonly OpRun[], all: readonly OpRun[], asOf: number, closed: boolean): string {
  const sum = (xs: readonly OpRun[]) => xs.reduce((a, r) => a + (r.cost_usd ?? 0), 0)
  const last30 = rows.filter(r => r.run.started_ts! > asOf - 30 * DAY)
  const secs = stripModel({ runs: cardRuns(all, asOf), points: [], now: asOf }).find(m => m.key === 'secs')!
  const time = secs.value === null ? [] : [`run time ${secs.value} (7d median${secs.delta ? `, ${secs.delta}` : ''})`]
  return [`Cost: ${usd(sum(inMonth))} ${closed ? 'month total' : 'month-to-date'}`, `${usd(sum(last30))} last 30d`, ...time].join(' · ')
}

/** The meta store's size at its newest scan and the change since the newest scan at least 30 days before (`over Nd`,
 *  N the span; `nominal.store_line`'s, over 30 days), then the store's scans. */
export function metaLine(input: MonthOpInput, cfg: MonthOpCfg, asOf: number): string {
  const all = `[All scans](${scansUrl(cfg)})`
  const { meta } = input
  if (!meta) return all
  const head = `[Metadata](${pageUrl(cfg, meta.store)})`
  if ('status' in meta.series) return `${head}: size unavailable (HTTP ${meta.series.status}) · ${all}`
  const pts = meta.series.points.filter(p => p.t <= asOf).sort((a, b) => a.t - b.t)
  const last = pts[pts.length - 1]
  if (!last) return `${head}: no scans yet · ${all}`
  const before = pts.filter(p => p.t <= last.t - 30 * DAY).pop()
  const change = before ? [`${si(last.bytes - before.bytes, true)} over ${Math.round((last.t - before.t) / DAY)}d`] : []
  return [`${head}: ${si(last.bytes)}`, ...change, all].join(' · ')
}

/** The links to the months either side (`[← October](…) · [December →](…)`), or nothing. */
export function linkLine(month: string, cfg: MonthOpCfg): string | null {
  const { index } = monthBounds(month)
  const prev = cfg.prevUrl ? `[← ${MONTHS[(index + 11) % 12]}](${cfg.prevUrl})` : null
  const next = cfg.nextUrl ? `[${MONTHS[(index + 1) % 12]} →](${cfg.nextUrl})` : null
  return [prev, next].filter(Boolean).join(' · ') || null
}

/** The OP's text: the status line (an open month's only), the month's counts, cost and run time, the metadata size,
 *  and the months either side. Lines, no trailing newline. */
export function monthOpText(input: MonthOpInput, cfg: MonthOpCfg): string {
  const { start, end, index } = monthBounds(input.month)
  if (input.now < start) throw new Error(`month ${input.month} hasn't started`)
  const closed = input.now >= end
  const asOf = closed ? end - 1 : input.now
  const all = input.runs.filter(r => r.run.started_ts != null && r.run.started_ts <= asOf)
  const rows = all.filter(r => r.run.kind === SCAN_KIND && !r.run.parent && r.run.status !== 'nop')
    .sort((a, b) => b.run.started_ts! - a.run.started_ts! || (a.run.run_id < b.run.run_id ? 1 : a.run.run_id > b.run.run_id ? -1 : 0))
  const inMonth = rows.filter(r => r.run.started_ts! >= start)
  return [
    ...(closed ? [] : [statusLine(rows, cfg)]),
    monthLine(inMonth, closed ? MONTHS[index] : 'This month', closed),
    costLine(rows, inMonth, all, asOf, closed),
    metaLine(input, cfg, asOf),
    ...[linkLine(input.month, cfg)].filter((l): l is string => l !== null),
  ].join('\n')
}
