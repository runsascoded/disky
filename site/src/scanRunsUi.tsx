// The scan-run pieces /scans and /health share (specs/done/scan-runs-ui.md, specs/done/health-page.md): the run list's fetch
// (one React Query cache entry for both pages), the status chip, the phase bar and its colors.
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ago } from './colors'
import { Tooltip } from './Tooltip'
import { fmtDur as fmtDurM } from './runs'
import {
  BEHIND, compactSpans, DEFERRED, issueLeaves, issueNext, issueState, issueWhat, jobTarget, outcomeParts, overlappingFailures, PASSING, phaseFamilies, phaseFamily, phaseFill, phaseOutcome,
  reasonJob, runOutcome, scanRunOf, type PhaseIssue, type PhaseSpan, type RunSummary, type ScanRun,
} from './scanRunsModel'
import { fmtTimestamp } from './scan'
import { DEFAULT_STORE, STORES, type Store, storeQuery, storeUrl } from './stores'

export interface Links { project: string | null; meta: string | null }
/** What would refuse "Run a scan" now (`/api/scan-runs`' `dispatch`); null where the deployment has no such action. */
export interface DispatchBlock { running: string | null; pending_since: number | null }
export type ListResponse = Links & { configured: boolean; runs: RunSummary[]; dispatch?: DispatchBlock | null }

export async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'include' })
  const text = await r.text()
  let data: unknown = null
  try { data = JSON.parse(text) } catch { data = null }
  if (!r.ok) throw new Error((data as { error?: string } | null)?.error ?? `${r.status} ${r.statusText}`)
  return data as T
}

/** The store `/scans` shows: its `?store=<key>` among the configured stores, else the primary (an unknown key too,
 * so a stale link still lands on runs). */
export function scansStore(search: string, stores: Store[] = STORES): Store {
  const key = new URLSearchParams(search).get('store')
  return stores.find(s => s.key === key) ?? stores[0]
}

/** A scan-runs page link (the list, or run `id`) for `store`: under its page path, as its map is (`/scans` for the
 *  primary, `/meta/scans` for the meta store). */
export function scansHref(store: Store, id?: string, primary: Store = DEFAULT_STORE): string {
  const base = store === primary ? '' : store.path.replace(/\/$/, '')
  return `${base}/scans${id ? `/${encodeURIComponent(id)}` : ''}`
}

/** `/api/scan-runs[/<id>]` as `store` fetches it (`storeUrl`). */
export const scanRunsApi = (store: Store, id?: string, primary: Store = DEFAULT_STORE): string =>
  storeUrl(`/api/scan-runs${id ? `/${encodeURIComponent(id)}` : ''}`, store, primary)

/** The run list of `store` (default the primary: /health's), one cache entry per store. */
export const useScanRuns = (store: Store = DEFAULT_STORE) => useQuery<ListResponse, Error>({
  queryKey: ['scan-runs', store.key], queryFn: () => getJson(scanRunsApi(store)), refetchInterval: 60_000, staleTime: 15_000,
})

/** The run that produced `scan` in `store`, once the store's runs load (null: not loaded, or none recorded it). */
export function useScanRunOf(store: Store, scan: string | null | undefined): ScanRun | null {
  const { data } = useScanRuns(store)
  return (data && scan && scanRunOf(data.runs, scan)) || null
}

/** `fmtDur`, with seconds under a minute (a 3 s publish isn't `0m`). */
export const fmtDur = (s: number): string => (s < 60 ? `${Math.round(s)}s` : fmtDurM(s))

/** How long ago epoch seconds `ts` was, to two units: `42s`, `17m`, `2h 31m`, `3d 4h`. */
export const agoLong = (ts: number, now: number = Date.now() / 1000): string => {
  const s = Math.max(0, Math.floor(now - ts))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}m`
  return `${Math.floor(s / 86400)}d ${Math.floor(s % 86400 / 3600)}h`
}

export const fmtRunTime = (ts: number | null, timeZone?: string): string =>
  ts == null ? '—' : fmtTimestamp(new Date(ts * 1000).toISOString(), timeZone)

/** A run's start, short, in the viewer's zone: `Thu Oct 16 · 3:16 AM` (the year only when it isn't `now`'s). */
export const fmtRunShort = (ts: number, timeZone?: string, now: number = Date.now() / 1000): string => {
  const at = new Date(ts * 1000)
  const year = (t: Date) => t.toLocaleDateString('en-US', { year: 'numeric', timeZone })
  const day = at.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', ...(year(at) === year(new Date(now * 1000)) ? {} : { year: 'numeric' }), timeZone })
  const time = at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone })
  return `${day.replaceAll(',', '')} · ${time}`
}

/** A run's start as a table cell's content: the short local time (linked to `href` when given), its age dim beside it, and the
 * scan id with the full timestamp in a tooltip. */
export function RunWhen({ ts, scan, href }: { ts: number | null; scan: string; href?: string }) {
  const label = ts == null ? <code>{scan}</code> : <>{fmtRunShort(ts)}</>
  return (
    <Tooltip content={<><div><code>{scan}</code></div>{ts != null && <div>{fmtRunTime(ts)}</div>}</>}>
      <span className="run-when">{href ? <Link to={href}>{label}</Link> : label}{ts != null && <span className="dim"> · {ago(ts)} ago</span>}</span>
    </Tooltip>
  )
}

export const runDay = (ts: number, timeZone?: string): string =>
  new Date(ts * 1000).toLocaleDateString('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone })
const PALETTE = ['--s1', '--s2', '--s4', '--s7', '--s5', '--s6', '--s3', '--s8']
/** A stable color per phase name: its family's hue (`phaseFamily`; the family's position in the usual phase order),
 *  and within a family of several (`list <bucket>`, …) a shade of it per member, darker to lighter in that order. A
 *  family's own key (a legend item) takes its plain hue. */
export function colorer(order: string[]): (phase: string) => string {
  const fams = phaseFamilies(order)
  const at = new Map<string, string>()
  fams.forEach(({ family, members }, i) => {
    const hue = `var(${PALETTE[i % PALETTE.length]})`
    at.set(family, hue)
    if (members.length > 1) members.forEach((m, j) => at.set(m, familyShade(hue, j, members.length)))
    else at.set(members[0], hue)
  })
  return phase => at.get(phase) ?? at.get(phaseFamily(phase)) ?? `var(${PALETTE[0]})`
}

/** Member `j` of `n`'s shade of `hue`: mixed toward black (the first) through the plain hue to white (the last), at
 *  most 35% either way, so the family reads as one color. */
export function familyShade(hue: string, j: number, n: number): string {
  const t = n > 1 ? j / (n - 1) : 0.5
  const pct = Math.round(Math.abs(t - 0.5) * 70)
  return pct === 0 ? hue : `color-mix(in oklab, ${hue}, ${t < 0.5 ? 'black' : 'white'} ${pct}%)`
}

/** The tip on a quiet run's warning (and a lost run's status). */
export const QUIET_TIP = "The run hasn't written its record (phases, its once-a-minute heartbeat) for this long: its job may have stalled or died. Check its Batch job."

/** Where an issue stands: the run that did its work since (linked through `runHref`; a later run's converging step
 * only shows that step completed there, not that it did this scan's work, so it reads "held since"); for a failed check, the first
 * later run whose same check passed (`PhaseIssue.passing`: no re-check of this scan); else whether a later run will
 * (`issueNext`). */
export function IssueStanding({ issue: i, runHref }: { issue: PhaseIssue; runHref?: (id: string) => string }) {
  const r = i.recovery ?? i.passing
  if (!r) return <>{issueNext(i)}</>
  const attempt = r.kind === 'phase-recovery'
  const label = attempt ? 'a later attempt' : <>the <code>{r.scan}</code> run</>
  const link = runHref ? <Link to={runHref(r.run_id)}>{label}</Link> : label
  if (!i.recovery) return <>Passing since {link}: that run's check of the site, not a re-check of this scan.</>
  if (!attempt) return <>Held since: a later run's <code>{i.phase}</code> step completed ({link}).</>
  return <>Done by {link}{r.finished_ts != null && <> at {fmtRunTime(r.finished_ts)}</>}.</>
}

/** Where a reason's job links (`jobTarget`): the runs to find its recorded run among, and the project its Batch page needs. */
export interface JobLinks { runs: readonly ScanRun[]; project: string | null }

/** `text`, with the Batch job it names (`reasonJob`: a busy skip's lease holder) linked to the run that job recorded
 * (`runHref`), else to its Batch console page; plain where `jobs` isn't given or there is nowhere to go. */
export function ReasonText({ text, jobs, runHref }: { text: string; jobs?: JobLinks; runHref?: (id: string) => string }) {
  const j = reasonJob(text)
  const to = j && jobs ? jobTarget(j.job, j.region, jobs.runs, jobs.project) : null
  if (!j || !to || ('run' in to && !runHref)) return <>{text}</>
  const name = <code>{j.job}</code>
  return <>{text.slice(0, j.start)}{'run' in to
    ? <Link to={runHref!(to.run)}>{name}</Link>
    : <a href={to.href} target="_blank" rel="noreferrer">{name} ↗</a>}{text.slice(j.end)}</>
}

/** A succeeded run's issues, one block each: what happened (with the recorded verdict when it has one; a job it names
 * linked through `jobs`), what it leaves short while nothing has done its work nor passed its check since (its step's
 * `leaves`), and where it stands. */
export function IssueList({ issues, runHref, jobs }: { issues: readonly PhaseIssue[]; runHref?: (id: string) => string; jobs?: JobLinks }) {
  return <div className="sr-issues">{issues.map(i => {
    const more = i.outcome === BEHIND || i.outcome === DEFERRED ? i.reason : i.detail
    return <div key={i.phase} className={issueState(i)}>
      <div><b>{i.phase}</b> <ReasonText text={issueWhat(i)} jobs={jobs} runHref={runHref} /></div>
      {issueLeaves(i) && <div>{issueLeaves(i)}</div>}
      {more && <div className="dim">{more}</div>}
      <div className="dim"><IssueStanding issue={i} runHref={runHref} /></div>
    </div>
  })}</div>
}

/** A short run's status tip's first line, by outcome. */
export const OUTCOME_HEAD: Record<string, string> = {
  incomplete: "Published, but the scan's data or serving is short.",
  [BEHIND]: "Data published. Search doesn't cover this scan yet.",
  [DEFERRED]: "Data published. A compaction held the interval store's append; a later run's append takes this scan.",
  [PASSING]: "A check failed on this run. The same check has passed in a later run: the site passes since, which doesn't show this scan did.",
  partial: "The scan's data and serving are complete; a side step failed.",
  recovered: 'Recovered: see below.',
}
/** A cancelled run's tip: what it means. */
export const CANCELLED_TIP = 'Cancelled: stopped on purpose, not a failure.'

const OUTCOME_CLASS: Record<string, string> = { incomplete: 'warn', [BEHIND]: 'behind', [DEFERRED]: 'behind', [PASSING]: 'passing', partial: 'partial', recovered: 'succeeded' }

/** `cell`: rendered as a table cell's content (`Tooltip`'s `cell`: the cell is the hover target, the tip to its right).
 * `lost`: a running run quiet past `LOST_S` (`liveness`), shown `lost?` until `scan-run reconcile` settles it.
 * A succeeded run with issues reads `<outcome> · <steps>`, or `<check> failed · passing since <scan>` (`outcomeParts`);
 * its tip lists every issue (`IssueList`) and pins on a click or a tap, so a phone reaches it and the recovering (or
 * passing) run's link (`runHref`) can be followed. */
export function Status({ run, failedAt, issues = [], cell, lost, runHref, jobs }: { run: ScanRun; failedAt?: string | null; issues?: PhaseIssue[]; cell?: boolean; lost?: boolean; runHref?: (id: string) => string; jobs?: JobLinks }) {
  if (run.status === 'running' && lost) {
    return <Tooltip cell={cell} content={<><div>{QUIET_TIP}</div><div className="dim">Its duration stops at its last write; a later run's start marks it failed once Batch confirms its job ended.</div></>}>
      <span className="sr-status lost">lost?</span>
    </Tooltip>
  }
  if (run.status === 'succeeded' && issues.length) {
    const outcome = runOutcome(run, issues)
    const { head, tail } = outcomeParts(run, issues)
    return <Tooltip cell={cell} pinnable content={<><div>{OUTCOME_HEAD[outcome]}</div><IssueList issues={issues} runHref={runHref} jobs={jobs} /></>}>
      <span className={`sr-status ${OUTCOME_CLASS[outcome]}`}>{head}{tail && <> <span className="sr-steps">· {tail}</span></>}</span>
    </Tooltip>
  }
  const tag = <span className={`sr-status ${run.status}`}>{run.status === 'nop' ? 'no-op' : run.status}</span>
  if (run.status === 'cancelled') return <Tooltip cell={cell} content={<><div>{CANCELLED_TIP}</div>{run.error && <div className="sr-err">{run.error}</div>}</>}>{tag}</Tooltip>
  if (run.status !== 'failed') return tag
  return <Tooltip cell={cell} content={<>{failedAt && <div>Failed {failedAt.startsWith('after') ? failedAt : `in ${failedAt}`}</div>}{run.error && <div className="sr-err">{run.error}</div>}</>}>{tag}</Tooltip>
}

/** A run's main-lane bar (an overlapped phase where the main lane has none: `compactSpans`), failure stripes for
 *  overlapping attempts, and all phases in its tip. `isolate`: a legend item hovered or pinned, whose spans (every
 *  lane's) stay lit over the rest dimmed. `spans` arrive filtered by the page's phase filter. */
export function PhaseBar({ spans, secs, color, width = 160, cell, isolate }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string; width?: number; cell?: boolean; isolate?: ((phase: string) => boolean) | null }) {
  if (!secs) return <span className="dim">—</span>
  const x = (t: number) => (t / secs) * width
  const main = compactSpans(spans)
  const lit = isolate ? spans.filter(s => isolate(s.phase)) : []
  return (
    <Tooltip cell={cell} content={spans.length ? <table className="sr-tip"><tbody>{spans.map((s, i) => <tr key={`${s.phase}-${i}`}><td><i className="sw" style={{ background: phaseFill(s, color) }} />{s.phase}{s.lane ? ' (beside)' : ''}{s.status !== 'done' ? ` · ${phaseOutcome(s)}` : ''}</td><td className="num">{fmtDur(s.end - s.start)}</td></tr>)}</tbody></table> : 'No phase markers recorded for this run'}>
      <svg className={`sr-bar${isolate ? ' isolating' : ''}`} width={width} height={10} viewBox={`0 0 ${width} 10`} role="img" aria-label={`${fmtDur(secs)} in ${main.length} phases`}>
        <rect className="sr-bar-bg" x={0} y={0} width={width} height={10} rx={2} />
        {main.map((s, i) => <rect key={`${s.phase}-${i}`} className={isolate ? 'dim' : undefined} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={10} style={{ fill: phaseFill(s, color) }} />)}
        {lit.map((s, i) => <rect key={`lit-${s.phase}-${i}`} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={10} style={{ fill: phaseFill(s, color) }} />)}
        {overlappingFailures(spans).map(s => <rect key={`failed-${s.phase}`} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={3} style={{ fill: 'var(--danger)' }} />)}
        {spans.filter(s => s.status === 'running').map(s => <rect key={`active-${s.phase}`} x={x(s.start)} y={7} width={Math.max(1, x(s.end) - x(s.start))} height={2} style={{ fill: color(s.phase) }} />)}
      </svg>
    </Tooltip>
  )
}
