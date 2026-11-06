// The scan-run pieces /scans and /health share (specs/done/scan-runs-ui.md, specs/done/health-page.md): the run list's fetch
// (one React Query cache entry for both pages), the status chip, the phase bar and its colors.
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ago } from './colors'
import { Tooltip } from './Tooltip'
import { fmtDur as fmtDurM } from './runs'
import {
  BEHIND, issueLeaves, issueNext, issueState, issueWhat, outcomeSteps, overlappingFailures, phaseFill, phaseOutcome, runOutcome, runOutcomeLabel, scanRunOf,
  type PhaseIssue, type PhaseSpan, type RunSummary, type ScanRun,
} from './scanRunsModel'
import { fmtTimestamp } from './scan'
import { DEFAULT_STORE, STORES, type Store, storeQuery, storeUrl } from './stores'

export interface Links { project: string | null; meta: string | null }
export type ListResponse = Links & { configured: boolean; runs: RunSummary[] }

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
/** A stable color per phase name: its position in the usual phase order. */
export const colorer = (order: string[]) => (phase: string) => `var(${PALETTE[Math.max(0, order.indexOf(phase)) % PALETTE.length]})`

/** The tip on a quiet run's warning (and a lost run's status). */
export const QUIET_TIP = "The run hasn't written its record (phases, its once-a-minute heartbeat) for this long: its job may have stalled or died. Check its Batch job."

/** Where an issue stands: the run that did its work since (linked through `runHref`), else whether a later run will
 * (`issueNext`). */
export function IssueStanding({ issue: i, runHref }: { issue: PhaseIssue; runHref?: (id: string) => string }) {
  const r = i.recovery
  if (!r) return <>{issueNext(i)}</>
  const attempt = r.kind === 'phase-recovery'
  const label = attempt ? 'a later attempt' : <>the <code>{r.scan}</code> run</>
  return <>Done by {runHref ? <Link to={runHref(r.run_id)}>{label}</Link> : label}{attempt && r.finished_ts != null && <> at {fmtRunTime(r.finished_ts)}</>}.</>
}

/** A succeeded run's issues, one block each: what happened (with the recorded verdict when it has one), what it
 * leaves short while nothing has done its work (its step's `leaves`), and where it stands. */
export function IssueList({ issues, runHref }: { issues: readonly PhaseIssue[]; runHref?: (id: string) => string }) {
  return <div className="sr-issues">{issues.map(i => {
    const more = i.outcome === BEHIND ? i.reason : i.detail
    return <div key={i.phase} className={issueState(i)}>
      <div><b>{i.phase}</b> {issueWhat(i)}</div>
      {issueLeaves(i) && <div>{issueLeaves(i)}</div>}
      {more && <div className="dim">{more}</div>}
      <div className="dim"><IssueStanding issue={i} runHref={runHref} /></div>
    </div>
  })}</div>
}

/** A short run's status tip's first line, by outcome. */
const OUTCOME_HEAD: Record<string, string> = {
  incomplete: "Published, but the scan's data or serving is short.",
  [BEHIND]: "Data published. Search doesn't cover this scan yet.",
  partial: "The scan's data and serving are complete; a side step failed.",
  recovered: 'Complete: what this run left short was done since.',
}
const OUTCOME_CLASS: Record<string, string> = { incomplete: 'warn', [BEHIND]: 'behind', partial: 'partial', recovered: 'succeeded' }

/** `cell`: rendered as a table cell's content (`Tooltip`'s `cell`: the cell is the hover target, the tip to its right).
 * `lost`: a running run quiet past `LOST_S` (`liveness`), shown `lost?` until `scan-run reconcile` settles it.
 * A succeeded run with issues reads `<outcome> · <steps>` (`runOutcomeLabel`); its tip lists every issue (`IssueList`)
 * and pins on a click or a tap, so a phone reaches it and the recovering run's link (`runHref`) can be followed. */
export function Status({ run, failedAt, issues = [], cell, lost, runHref }: { run: ScanRun; failedAt?: string | null; issues?: PhaseIssue[]; cell?: boolean; lost?: boolean; runHref?: (id: string) => string }) {
  if (run.status === 'running' && lost) {
    return <Tooltip cell={cell} content={<><div>{QUIET_TIP}</div><div className="dim">Its duration stops at its last write; a later run's start marks it failed once Batch confirms its job ended.</div></>}>
      <span className="sr-status lost">lost?</span>
    </Tooltip>
  }
  if (run.status === 'succeeded' && issues.length) {
    const outcome = runOutcome(run, issues)
    const steps = outcomeSteps(issues, outcome)
    return <Tooltip cell={cell} pinnable content={<><div>{OUTCOME_HEAD[outcome]}</div><IssueList issues={issues} runHref={runHref} /></>}>
      <span className={`sr-status ${OUTCOME_CLASS[outcome]}`}>{outcome}{steps.length > 0 && <> <span className="sr-steps">{runOutcomeLabel(run, issues).slice(outcome.length + 1)}</span></>}</span>
    </Tooltip>
  }
  const tag = <span className={`sr-status ${run.status}`}>{run.status === 'nop' ? 'no-op' : run.status}</span>
  if (run.status !== 'failed') return tag
  return <Tooltip cell={cell} content={<>{failedAt && <div>Failed {failedAt.startsWith('after') ? failedAt : `in ${failedAt}`}</div>}{run.error && <div className="sr-err">{run.error}</div>}</>}>{tag}</Tooltip>
}

/** A run's main-lane bar, failure stripes for overlapping attempts, and all phases in its tip. */
export function PhaseBar({ spans, secs, color, width = 160, cell }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string; width?: number; cell?: boolean }) {
  if (!secs) return <span className="dim">—</span>
  const x = (t: number) => (t / secs) * width
  const main = spans.filter(s => s.lane === 0)
  return (
    <Tooltip cell={cell} content={spans.length ? <table className="sr-tip"><tbody>{spans.map(s => <tr key={s.phase}><td><i className="sw" style={{ background: phaseFill(s, color) }} />{s.phase}{s.lane ? ' (beside)' : ''}{s.status !== 'done' ? ` · ${phaseOutcome(s)}` : ''}</td><td className="num">{fmtDur(s.end - s.start)}</td></tr>)}</tbody></table> : 'No phase markers recorded for this run'}>
      <svg className="sr-bar" width={width} height={10} viewBox={`0 0 ${width} 10`} role="img" aria-label={`${fmtDur(secs)} in ${main.length} phases`}>
        <rect className="sr-bar-bg" x={0} y={0} width={width} height={10} rx={2} />
        {main.map(s => <rect key={s.phase} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={10} style={{ fill: phaseFill(s, color) }} />)}
        {overlappingFailures(spans).map(s => <rect key={`failed-${s.phase}`} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={3} style={{ fill: 'var(--danger)' }} />)}
        {spans.filter(s => s.status === 'running').map(s => <rect key={`active-${s.phase}`} x={x(s.start)} y={7} width={Math.max(1, x(s.end) - x(s.start))} height={2} style={{ fill: color(s.phase) }} />)}
      </svg>
    </Tooltip>
  )
}
