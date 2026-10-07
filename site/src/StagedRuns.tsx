// /staged's runs section (specs/staged-runs.md): one row per run from the
// moment it is dispatched — the Batch job joined to the D1 run its executor
// records — with what it planned, its live progress, its totals, its undo
// window, its files and logs, and the controls the deployment's executor
// offers (`CAPS`). A row expands into the run's detail: D1 totals, the
// planned set, the logged decisions per bucket, the checks between them, and
// its bands.
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { stringParam, useUrlState } from 'use-prms'
import { FaRegCopy } from 'react-icons/fa6'
import { copyText } from './CopyName'
import { Tooltip } from './Tooltip'
import { UserChip } from './UserChip'
import { TimeCell } from './PrefixTable'
import { RunOutcome, runOutcomeLabel } from './RunOutcome'
import { Link } from 'react-router-dom'
import { RunProgress } from './RunProgress'
import { RunBucketProgress } from './RunBucketProgress'
import { RunArtifacts, RunPlan } from './RunArtifacts'
import { RunBands } from './RunBands'
import { RunChecks } from './RunChecks'
import { runHref } from './runHistory'
import { conservativeDeadline } from './prefixExecution'
import { percentOf } from './runChart'
import { selectRuns } from './runTable'
import { fmtN } from './types'
import { DEFAULT_STORE } from './stores'
import { CAPS, type RunAction, useRunDetail, useRunFiles } from './plans'
import {
  type Check, commonBucketPrefix, type DeletionRun, elapsed, elapsedDetails, type ExecJob, fmtDur, type LogSummaryFile, loggedOf, logSubdir,
  type PlanSummaryFile, plannedOf, progressEstimate, type ProgressFile, runControls, runFileHref, runFilesRel, runLabel, type RunView, sumProgress, type Tally,
  verifyRun, viewBuckets, viewLive, viewState, joinRuns,
} from './runs'

const PAGE_SIZES = [20, 50, Infinity]
const utc = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ')
const BUCKET_PREFIX = commonBucketPrefix(DEFAULT_STORE.buckets)
const shortBucket = (b: string): string => (BUCKET_PREFIX && b.startsWith(BUCKET_PREFIX) ? b.slice(BUCKET_PREFIX.length) : b)

const rowId = (key: string): string => `run-${key.replace(/[^A-Za-z0-9_-]+/g, '-')}`

function RunId({ id, href }: { id: string; href?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => copyText(id).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })
  return (
    <span className="run-id">
      <Tooltip content={<code className="elide-full">{id}</code>}>{href ? <Link to={href}><code>{runLabel(id)}</code></Link> : <code>{runLabel(id)}</code>}</Tooltip>
      <Tooltip content={copied ? 'Copied ✓' : 'Copy full run ID'}>
        <button type="button" className="run-copy" aria-label="Copy full run ID" onClick={copy}>
          {copied ? <span className="copied">✓</span> : <FaRegCopy aria-hidden />}
        </button>
      </Tooltip>
    </span>
  )
}

/** The run dir a view reads its files from (`runFilesRel`), when it has one. */
const dirOf = (v: RunView): string | null => {
  const d = v.run?.log_dir ?? v.job?.plan ?? v.job?.run
  return d ? runFilesRel(d) : null
}

export function RunsSection({ runs, jobs, configured, jobsError, admin, busy, act, fmtBytes, refreshing }: {
  runs: DeletionRun[]
  jobs: ExecJob[]
  configured: boolean
  jobsError: Error | null
  admin: boolean
  busy: boolean
  act: (a: RunAction) => void
  fmtBytes: (b: number) => string
  refreshing: boolean
}) {
  const views = useMemo(() => joinRuns(runs, jobs), [runs, jobs])
  // What each run planned (`plan-summary.json`, absent until its manifest
  // step ran), and each live run's per-bucket progress.
  const planRels = views.flatMap(v => { const d = dirOf(v); return d ? [`${d}plan-summary.json`] : [] })
  const liveRels = new Set(views.filter(viewLive).flatMap(v => { const d = dirOf(v); return d ? [`${d}plan-summary.json`] : [] }))
  const planQs = useRunFiles<PlanSummaryFile>(planRels, rel => liveRels.has(rel), { poll: 60_000, untilPresent: true })
  const planned = new Map<string, { file: PlanSummaryFile; tally: Tally }>()
  planRels.forEach((rel, i) => { const f = planQs[i]?.data; if (f) planned.set(rel, { file: f, tally: plannedOf(f) }) })
  const planOf = (v: RunView) => { const d = dirOf(v); return d ? planned.get(`${d}plan-summary.json`) : undefined }
  const progTargets = views.filter(viewLive).flatMap(v => {
    const d = dirOf(v)
    if (!d) return []
    const bs = viewBuckets(v).length ? viewBuckets(v) : Object.keys(planOf(v)?.file.buckets ?? {})
    return bs.map(b => ({ key: v.key, rel: `${d}progress/${b}.json` }))
  })
  const progQs = useRunFiles<ProgressFile>(progTargets.map(t => t.rel), true)
  const progress = new Map<string, ProgressFile[]>()
  progTargets.forEach((t, i) => { const p = progQs[i]?.data; if (p) progress.set(t.key, [...(progress.get(t.key) ?? []), p]) })

  const [pageSize, setPageSize] = useState(PAGE_SIZES[0])
  const [page, setPage] = useState(0)
  const [userFilter, setUserFilter] = useUrlState('ru', stringParam(''))
  const [modeFilter, setModeFilter] = useUrlState('rm', stringParam(''))
  const [stateFilter, setStateFilter] = useUrlState('rs', stringParam(''))
  const [queryFilter, setQueryFilter] = useUrlState('rq', stringParam(''))
  const [sort, setSort] = useUrlState('sort', stringParam('started'))
  const [direction, setDirection] = useUrlState('order', stringParam('desc'))
  const now = Math.floor(Date.now() / 1000)
  const metrics = new Map(views.map(v => {
    const ps = progress.get(v.key), p = ps ? sumProgress(ps) : null
    const deleted = v.run?.finished_ts ? v.run.deleted_objects : p?.deletes
    const secs = elapsed(v, now)
    return [v.key, { planned: planOf(v)?.tally.objects, deleted, bytes: v.run?.finished_ts ? v.run.deleted_bytes : p?.bytes, rate: deleted != null && secs != null && secs > 0 ? deleted / secs : undefined }]
  }))
  const filtered = selectRuns(views, { user: userFilter ?? '', mode: modeFilter ?? '', state: stateFilter ?? '', query: queryFilter ?? '', sort: sort ?? 'started', ascending: direction === 'asc' }, metrics, now)
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const pg = Math.min(page, pages - 1)
  const shown = filtered.slice(pg * pageSize, (pg + 1) * pageSize)
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set())
  const cols = 17
  const sortButton = (key: string, label: string) => <button type="button" className="run-sort" onClick={() => { setSort(key); setDirection(sort === key && direction === 'desc' ? 'asc' : 'desc'); setPage(0) }}>{label}{sort === key ? direction === 'asc' ? ' ▴' : ' ▾' : ''}</button>

  // `?run=<id>` (a run id, or its Batch job's): page to that row, open its
  // detail, scroll to it — once, when it first appears.
  const [runP, setRunP] = useUrlState('run', stringParam())
  const landed = useRef<string | null>(null)
  const target = runP ? filtered.findIndex(v => v.key === runP || v.job?.job_id === runP) : -1
  useEffect(() => {
    if (!runP) { landed.current = null; return }
    if (target < 0 || landed.current === runP) return
    landed.current = runP ?? null
    const v = filtered[target]
    setPage(Math.floor(target / pageSize))
    requestAnimationFrame(() => document.getElementById(rowId(v.key))?.scrollIntoView({ block: 'center' }))
  }, [target, runP, filtered, pageSize])

  if (!views.length) return null
  return (
    <section id="runs">
      <h3>Runs ({views.length}){refreshing && <span className="dim refreshing"> · refreshing…</span>}</h3>
      {!configured && <p className="dim">Dispatch isn't configured on this deployment (no executor credentials): only recorded runs are listed, without live state.</p>}
      {jobsError && <p className="err">Batch jobs: {jobsError.message}</p>}
      <div className="run-filters">
        <input aria-label="Filter runs" placeholder="filter runs…" value={queryFilter} onChange={e => { setQueryFilter(e.target.value); setPage(0) }} />
        <select aria-label="Filter runs by user" value={userFilter} onChange={e => { setUserFilter(e.target.value); setPage(0) }}><option value="">all users</option>{[...new Set(views.map(v => v.run?.actor ?? v.job?.by ?? '').filter(Boolean))].sort().map(u => <option key={u}>{u}</option>)}</select>
        <select aria-label="Filter run mode" value={modeFilter} onChange={e => { setModeFilter(e.target.value); setPage(0) }}><option value="">real + dry</option><option value="real">real only</option><option value="dry">dry only</option></select>
        <select aria-label="Filter run state" value={stateFilter} onChange={e => { setStateFilter(e.target.value); setPage(0) }}><option value="">all states</option>{[...new Set(views.map(viewState))].sort().map(s => <option key={s} value={s}>{s.toLowerCase()}</option>)}</select>
        <label>sort <select aria-label="Sort runs" value={sort} onChange={e => { setSort(e.target.value); setPage(0) }}>{['started', 'by', 'mode', 'planned', 'deleted', 'bytes', 'state', 'rate', 'elapsed', 'gone', 'overwritten', 'drift', 'undo', 'buckets'].map(s => <option key={s}>{s}</option>)}</select></label>
        <button type="button" onClick={() => setDirection(direction === 'asc' ? 'desc' : 'asc')}>{direction === 'asc' ? 'ascending' : 'descending'}</button>
        <span className="dim">{filtered.length} / {views.length}</span>
      </div>
      {runP && target < 0 && <p className="dim">The linked run is hidden by these filters or has not been recorded yet.</p>}
      <div className="runs-wrap">
        <table className="runs">
          <thead>
            <tr>
              <th /><th>{sortButton('started', 'run')}</th><th>{sortButton('by', 'by')}</th><th>{sortButton('mode', 'mode')}</th>
              <th className="num"><Tooltip content="What the manifest found. Click to sort by planned object count; byte sorting is available in the sort menu.">{sortButton('planned', 'planned')}</Tooltip></th>
              <th className="num"><Tooltip content="Real runs: actual deletions. Dry runs: predictions only; nothing is deleted. Click to sort by object count.">{sortButton('deleted', 'deleted / would delete')}</Tooltip></th>
              <th>{sortButton('state', 'state')}</th><th><Tooltip content="Sparkline: interval rate over the last hour. Text: average over the run's entire elapsed time, including startup and pauses.">{sortButton('rate', 'rate / history')}</Tooltip></th><th className="num">{sortButton('elapsed', 'elapsed')}</th>
              <th className="num"><Tooltip content="Planned keys already gone when the run re-listed.">{sortButton('gone', 'gone')}</Tooltip></th>
              <th className="num"><Tooltip content="Planned keys rewritten since the scan (a new generation): left alone.">{sortButton('overwritten', 'overwritten')}</Tooltip></th>
              <th className="num"><Tooltip content="Directories that gained keys since the scan.">{sortButton('drift', 'drift')}</Tooltip></th>
              <th>{sortButton('started', 'started')}</th>
              <th><Tooltip content="Recorded run-level undo deadline. GCS retention starts separately at each object's deletion; this is not an object-level recovery guarantee.">{sortButton('undo', 'undo by')}</Tooltip></th>
              <th>links</th><th>{sortButton('buckets', 'buckets')}</th><th />
            </tr>
          </thead>
          <tbody>
            {shown.map(v => {
              const r = v.run
              const job = v.job
              const mode = r?.mode ?? (job?.mode === 'real' ? 'real' : 'dry')
              const state = viewState(v)
              const live = viewLive(v)
              const ctl = runControls(v, CAPS, admin, now)
              const plan = planOf(v)?.tally
              const prog = progress.get(v.key)
              const p = prog ? sumProgress(prog) : null
              const estimate = prog ? progressEstimate(prog, plan?.objects, now) : null
              const secs = elapsed(v, now)
              const startedTs = r?.started_ts ?? (job?.created ? Date.parse(job.created) / 1000 : null)
              const buckets = viewBuckets(v)
              const actor = r?.actor ?? job?.by ?? ''
              const dir = dirOf(v)
              const isOpen = v.key === filtered[target]?.key
              const toggle = () => setRunP(isOpen ? undefined : v.key)
              const failed = state === 'FAILED'
              const jobId = job?.job_id ?? v.key
              return (
                <Fragment key={v.key}>
                  <tr id={rowId(v.key)} className={['run', mode, state.toLowerCase(), failed ? 'failed' : '', live ? 'live' : '', isOpen ? 'linked' : ''].filter(Boolean).join(' ')}>
                    <td>{r && <button type="button" className="fold" aria-expanded={isOpen} aria-label={isOpen ? 'hide run detail' : 'show run detail'} onClick={toggle}>{isOpen ? '▾' : '▸'}</button>}</td>
                    <td className="rid"><RunId id={r?.run_id ?? jobId} href={r ? runHref(r.run_id) : undefined} /></td>
                    <td>{actor && <UserChip who={actor} size={16} />}</td>
                    <td><span className={`tag ${mode}`}>{mode === 'real' ? 'REAL' : 'DR'}</span></td>
                    <td className="num nb">{plan ? <>{fmtBytes(plan.bytes)} <span className="dim">· {fmtN(plan.objects)}</span></> : <span className="dim">—</span>}</td>
                    <td className="num nb">
                      {r?.finished_ts ? (
                        <>
                          <Tooltip content={mode === 'dry' ? 'Dry run only: no objects were deleted.' : 'Objects deleted by this real run.'}>
                            <RunOutcome mode={mode}>{fmtBytes(r.deleted_bytes)} <span className="dim">· {fmtN(r.deleted_objects)}</span></RunOutcome>
                          </Tooltip>
                          {r.freed_bytes != null && (
                            <Tooltip content={<>What deleting this set would <b>actually</b> free, measured on the laptop: bytes it shares with a clone or hardlink outside the set (e.g. a <code>.venv</code>’s files cloned from <code>~/.cache/uv</code>) stay on disk. The size before it counts every path in full.</>}>
                              <span className="frees"> · frees <b>{fmtBytes(r.freed_bytes)}</b></span>
                            </Tooltip>
                          )}
                        </>
                      ) : live && p ? (
                        <Tooltip content={<>
                          {mode === 'dry' && <div>Dry run only: no objects are being deleted.</div>}
                          {plan && estimate?.percent != null && <div>{estimate.percent.toFixed(2)}% accounted for · {fmtN(estimate.decided)} of {fmtN(plan.objects)} planned objects (deletes + skips/failures)</div>}
                          <div>{p.rate.toLocaleString('en-US')}/s {runOutcomeLabel(mode)} · {fmtN(p.roots_done)} / {fmtN(p.roots)} reported roots complete</div>
                          <div>{estimate?.secondsLeft != null ? `Rough ETA: ${estimate.secondsLeft < 60 ? '<1m' : fmtDur(estimate.secondsLeft)} remaining at the active bucket's average decision rate. Other buckets may differ; not a deadline.` : estimate?.stale ? 'ETA unavailable: progress sample is stale.' : 'ETA unavailable until enough progress is reported.'}</div>
                        </>}>
                          <RunOutcome mode={mode}><span className="prog"><progress max={plan?.objects || undefined} value={estimate?.decided ?? p.deletes} /> {fmtBytes(p.bytes)} · {fmtN(p.deletes)} · {p.rate.toLocaleString('en-US')}/s
                            {estimate?.percent != null && <span className="prog-estimate dim">{percentOf(estimate.decided, plan?.objects ?? 0)}{estimate.secondsLeft != null ? ` · ~${estimate.secondsLeft < 60 ? '<1m' : fmtDur(estimate.secondsLeft)} left` : estimate.stale ? ' · stale' : ''}</span>}
                          </span></RunOutcome>
                        </Tooltip>
                      ) : live && CAPS.runFiles && !plan ? (
                        <Tooltip content="The manifest step is still streaming the scan listing; deletes start once it lands."><span className="dim">planning…</span></Tooltip>
                      ) : live && CAPS.runFiles ? (
                        <Tooltip content="No progress file yet: deletes are landing, but only the final log will say how many."><span className="dim">no progress file</span></Tooltip>
                      ) : <span className="dim">—</span>}
                    </td>
                    <td>
                      <span className="rstate">{state.toLowerCase()}</span>
                      {state === 'CANCELLED' && <Tooltip content="A STOP request was recorded. Batch may report this clean interruption as FAILED (exit 130); acknowledged deletions remain logged."><span className="dim"> · intentional stop</span></Tooltip>}
                      {failed && job?.last_event && <details className="why"><summary>why</summary><div>{job.last_event}</div></details>}
                      {ctl.undoing && <span className="tag">undoing</span>}
                      {r?.undo_state === 'full' && <span className="tag">undone</span>}
                      {r?.undo_state === 'partial' && !ctl.undoing && <Tooltip content="An undo ran (or was dispatched) but not every deleted object is live again; undo again to retry the rest."><span className="tag">partly undone</span></Tooltip>}
                      {r?.purge_state === 'done' && <span className="tag">purged</span>}
                    </td>
                    <td>{CAPS.runFiles && r && <RunProgress id={r.run_id} live={live} compact fmtBytes={fmtBytes} lifetime={secs != null ? { objects: r.finished_ts ? r.deleted_objects : p?.deletes ?? 0, seconds: secs } : undefined} />}</td>
                    <td className="num"><Tooltip content={<>{elapsedDetails(v).map(line => <div key={line}>{line}</div>)}</>}><span>{secs == null ? <span className="dim">—</span> : fmtDur(secs)}</span></Tooltip></td>
                    <td className="num">{r?.finished_ts ? fmtN(r.skipped_gone) : p ? fmtN(p.gone) : <span className="dim">—</span>}</td>
                    <td className="num">{r?.finished_ts ? fmtN(r.skipped_overwritten ?? 0) : <span className="dim">—</span>}</td>
                    <td className="num">{r?.finished_ts ? fmtN((r.drift_dirs ?? 0) + (r.ledger_drift_dirs ?? 0)) : <span className="dim">—</span>}</td>
                    <td><TimeCell ts={startedTs ?? undefined} /></td>
                    <td className="nb">
                      {mode === 'real' && r?.undo_deadline ? (
                        <Tooltip content={`${utc(r.undo_deadline)} UTC`}>
                          <span className={now >= r.undo_deadline ? 'dim' : undefined}>{now >= r.undo_deadline ? 'closed' : fmtDur(r.undo_deadline - now)}</span>
                        </Tooltip>
                      ) : <span className="dim">—</span>}
                    </td>
                    <td className="nb links">
                      {r && <Link to={runHref(r.run_id)}>details</Link>}
                      {CAPS.runFiles && r && dir && <> · <Link to={runHref(r.run_id, 'plan')}>plan</Link></>}
                      {CAPS.runFiles && r && dir && (r.finished_ts || live) && <> · <Link to={runHref(r.run_id, 'log')}>log</Link></>}
                      {job?.logs && <>{CAPS.runFiles && dir && ' · '}<a href={job.logs} target="_blank" rel="noreferrer">Batch ↗</a></>}
                    </td>
                    <td className="nb">
                      {buckets.length ? buckets.map(shortBucket).join(', ') : 'all'}
                      {job?.bucket_region && job.region && job.bucket_region !== job.region && (
                        <Tooltip content={`Ran in ${job.region}, not the bucket's ${job.bucket_region} (Batch may not offer the bucket's region).`}><span className="dim"> · {job.region}</span></Tooltip>
                      )}
                    </td>
                    <td className="actions nb">
                      {ctl.stop && (
                        <Tooltip content={CAPS.stopHint}>
                          <button type="button" disabled={busy || stopping.has(jobId)} onClick={() => { setStopping(s => new Set([...s, jobId])); act({ action: 'stop', job_id: jobId }) }}>{stopping.has(jobId) ? 'stopping' : 'stop'}</button>
                        </Tooltip>
                      )}
                      {ctl.undo && r && (
                        <Tooltip content={`Restore what this run deleted (a Batch job; until ${r.undo_deadline ? `${utc(r.undo_deadline)} UTC` : 'its window closes'}).`}>
                          <button type="button" disabled={busy} onClick={() => act({ action: 'undo', run_id: r.run_id })}>undo</button>
                        </Tooltip>
                      )}
                      {ctl.purge && r && (
                        <Tooltip content="Permanently drop this run's deleted versions (irreversible; the undo window has closed).">
                          <button type="button" className="danger" disabled={busy} onClick={() => act({ action: 'purge', run_id: r.run_id })}>purge</button>
                        </Tooltip>
                      )}
                    </td>
                  </tr>
                  {isOpen && r && (
                    <tr className="run-detail-row"><td colSpan={cols}><RunDetail run={r} live={live} progress={p} dir={dir} planned={planOf(v)?.file ?? null} fmtBytes={fmtBytes} now={now} /></td></tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      {filtered.length > PAGE_SIZES[0] && (
        <div className="pg runs-pg">
          <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous runs">‹</button>
          <span>{pg * pageSize + 1}–{Math.min(filtered.length, (pg + 1) * pageSize)} of {filtered.length}</span>
          <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next runs">›</button>
          <span className="dim">per page</span>
          {PAGE_SIZES.map(n => <button key={n} type="button" className={n === pageSize ? 'on' : undefined} onClick={() => { setPageSize(n); setPage(0) }}>{n === Infinity ? 'all' : n}</button>)}
        </div>
      )}
    </section>
  )
}

const DECISION_ORDER = ['delete', 'skipped_gone', 'skipped_overwritten', 'delete_failed']

/** A run's detail: D1 totals + undo window, what it planned, what it logged
 * (per bucket), the checks between them, and its bands. */
export function RunDetail({ run, live, progress, dir, planned, fmtBytes, now }: {
  run: DeletionRun
  live: boolean
  progress: ReturnType<typeof sumProgress> | null
  dir: string | null
  planned: PlanSummaryFile | null
  fmtBytes: (b: number) => string
  now: number
}) {
  const detail = useRunDetail(run.run_id, true, live)
  const logRel = dir && run.finished_ts ? [`${dir}${logSubdir(run.mode)}-summary.json`] : []
  const [logQ] = useRunFiles<LogSummaryFile>(logRel, false)
  const log = logQ?.data ?? null
  const logged = log ? loggedOf(log) : null
  const plan = planned ? plannedOf(planned) : null
  const checks: Check[] = run.finished_ts ? verifyRun(detail.data?.run ?? run, plan, logged) : []
  const r = detail.data?.run ?? run
  const bands = detail.data?.bands ?? []
  const decisionKeys = logged ? [...DECISION_ORDER.filter(k => k in logged.decisions), ...Object.keys(logged.decisions).filter(k => !DECISION_ORDER.includes(k)).sort()] : []

  return (
    <div className="run-detail">
      {CAPS.runFiles && <RunProgress id={run.run_id} live={live} fmtBytes={fmtBytes} planned={planned} />}
      <dl className="rd-totals">
        {live ? <div><dt>live progress</dt><dd>{progress ? <>{fmtBytes(progress.bytes)} · {fmtN(progress.deletes)} {r.mode === 'real' ? 'deleted' : 'would delete'} · {fmtN(progress.gone)} gone</> : 'waiting for progress'}<span className="dim"> · final totals recorded when the run ends</span></dd></div>
          : <div><dt>final totals</dt><dd>{fmtBytes(r.deleted_bytes)} · {fmtN(r.deleted_objects)} {r.mode === 'real' ? 'deleted' : 'would delete'} · {fmtN(r.skipped_gone)} gone · {fmtN(r.skipped_overwritten ?? 0)} overwritten · {fmtN((r.drift_dirs ?? 0) + (r.ledger_drift_dirs ?? 0))} drifted dirs</dd></div>}
        <div><dt>scan</dt><dd>{r.scan ?? '—'}{r.plan_digest ? <> · items <code>{r.plan_digest}</code></> : null}</dd></div>
        {r.mode === 'real' && <div><dt>undo</dt><dd>
          {r.undo_deadline ? <>recorded run deadline {utc(r.undo_deadline)} UTC {now < r.undo_deadline ? <span className="dim">(in {fmtDur(r.undo_deadline - now)})</span> : <span className="dim">(closed)</span>}</> : live ? 'recovery after stop and drain; final deadline not recorded yet' : 'no window recorded'}
          {' · '}state <b>{r.undo_state}</b>{r.purge_state && r.purge_state !== 'none' && <> · purge <b>{r.purge_state}</b></>}
        </dd></div>}
        {r.log_dir && <div><dt>run dir</dt><dd><code>{r.log_dir}</code></dd></div>}
      </dl>
      {plan && <p className="run-completion"><b>{percentOf(live ? progress?.deletes ?? 0 : r.deleted_objects, plan.objects)} objects</b> · <b>{percentOf(live ? progress?.bytes ?? 0 : r.deleted_bytes, plan.bytes)} bytes</b> {r.mode === 'dry' ? 'would delete' : 'acknowledged deleted'} of {fmtN(plan.objects)} objects / {fmtBytes(plan.bytes)} planned.</p>}
      {planned && dir && <RunBucketProgress planned={planned} dir={dir} live={live} log={log} fmtBytes={fmtBytes} dry={r.mode === 'dry'} bands={bands} />}
      {r.mode === 'real' && <section id="recovery" className="rd-recovery">
        <h4>Recovery</h4>
        <p>{live ? 'Stop and drain this run before restoring. Its committed decision logs retain exact generations; a restore during deletion could be deleted again.' : 'The web undo control restores the entire run. The CLI can restrict recovery to a selected prefix.'}</p>
        <p className="dim">Retention starts separately when each object is deleted, not at run completion. Empty at a scan does not establish that this run deleted it.{conservativeDeadline(r) != null && <> Conservative cutoff for this run: {utc(conservativeDeadline(r)!)} UTC. Later deletions may remain recoverable longer; the stored run deadline is not an object-level guarantee.</>}</p>
        <code>dt-cloud sweep undo -p gs://BUCKET/PREFIX/ {r.run_id}</code>
      </section>}

      {CAPS.runFiles && (
        <div className="rd-files">
          <h4 id="plan">Planned</h4>
          {dir && <p><a href={runFileHref(`${dir}plan.json`)} target="_blank" rel="noreferrer">Download plan JSON ↗</a></p>}
          {dir && <RunPlan dir={dir} />}
          {planned && plan ? (
            <p>{fmtBytes(plan.bytes)} · {fmtN(plan.objects)} objects under the plan's prefixes{planned.total?.outside_bands ? <span className="dim"> · {fmtN(planned.total.outside_bands.objects)} listed keys outside them</span> : null}</p>
          ) : <p className="dim">{live ? 'the manifest step hasn\'t written its summary yet' : 'no plan summary'}</p>}
          <h4 id="log">Logged decisions</h4>
          {dir && <RunArtifacts dir={`${dir}${logSubdir(run.mode)}/`} buckets={Object.keys(planned?.buckets ?? {})} />}
          {log && logged ? (
            <div className="runs-wrap">
              <table className="runs rd-log">
                <thead><tr><th>bucket</th>{decisionKeys.map(k => <th key={k} className="num">{k === 'delete' ? runOutcomeLabel(run.mode) : k.replace(/_/g, ' ')}</th>)}<th className="num">bytes</th><th className="num">drifted dirs</th><th className="num">failed dirs</th><th>notes</th></tr></thead>
                <tbody>
                  {Object.entries(log.buckets).map(([b, s]) => (
                    <tr key={b}>
                      <td><code>{b}</code></td>
                      {decisionKeys.map(k => <td key={k} className="num">{fmtN(s.decisions?.[k] ?? 0)}</td>)}
                      <td className="num">{fmtBytes(s.delete_bytes ?? 0)}</td>
                      <td className="num">{fmtN(s.drift_dirs?.length ?? 0)}</td>
                      <td className="num">{fmtN(s.failed_dirs?.length ?? 0)}</td>
                      <td className="dim">
                        {s.soft_delete_days != null && <>soft delete {Math.round(s.soft_delete_days)}d</>}
                        {s.interrupted && <> · stopped with {fmtN(s.interrupted.roots_skipped)} of {fmtN(s.interrupted.roots)} roots not started</>}
                        {s.missing_perms?.length ? <> · missing {s.missing_perms.join(', ')}</> : null}
                      </td>
                    </tr>
                  ))}
                  {Object.keys(log.buckets).length > 1 && (
                    <tr className="total"><td>all</td>{decisionKeys.map(k => <td key={k} className="num">{fmtN(logged.decisions[k] ?? 0)}</td>)}<td className="num">{fmtBytes(logged.bytes)}</td><td className="num">{fmtN(logged.driftDirs)}</td><td className="num">{fmtN(logged.failedDirs)}</td><td /></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : <p className="dim">{run.finished_ts ? 'no decision summary in the run dir' : 'Final summary is written when the run ends; decision parts appear as each bucket executes.'}</p>}
        </div>
      )}

      {CAPS.runFiles && <RunChecks checks={checks} live={live} />}

      <h4>Bands ({bands.length})</h4>
      {detail.isLoading ? <p className="dim">loading…</p> : detail.error ? <p className="err">{detail.error.message}</p> : !bands.length ? <p className="dim">{run.finished_ts ? 'no band rows' : 'recorded when the run ends'}</p> : (
        <RunBands bands={bands} id={r.run_id} fmtBytes={fmtBytes} />
      )}
    </div>
  )
}
