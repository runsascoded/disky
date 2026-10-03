// /staged's runs section (specs/staged-runs.md): one row per run from the
// moment it is dispatched — the Batch job joined to the D1 run its executor
// records — with what it planned, its live progress, its totals, its undo
// window, its files and logs, and the controls the deployment's executor
// offers (`CAPS`). A row expands into the run's detail: D1 totals, the
// planned set, the logged decisions per bucket, the checks between them, and
// its bands.
import { Fragment, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { UserChip } from './UserChip'
import { TimeCell } from './PrefixTable'
import { fmtN } from './types'
import { DEFAULT_STORE } from './stores'
import { CAPS, type RunAction, useRunDetail, useRunFiles } from './plans'
import {
  type Check, commonBucketPrefix, type DeletionRun, elapsed, type ExecJob, fmtDur, type LogSummaryFile, loggedOf, logSubdir,
  type PlanSummaryFile, plannedOf, type ProgressFile, runControls, runFilesRel, type RunView, sumProgress, type Tally,
  verifyRun, viewBuckets, viewLive, viewState, joinRuns,
} from './runs'

const PAGE_SIZES = [20, 50, Infinity]
const utc = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ')
const BUCKET_PREFIX = commonBucketPrefix(DEFAULT_STORE.buckets)
const shortBucket = (b: string): string => (BUCKET_PREFIX && b.startsWith(BUCKET_PREFIX) ? b.slice(BUCKET_PREFIX.length) : b)

/** The run dir a view reads its files from (`runFilesRel`), when it has one. */
const dirOf = (v: RunView): string | null => {
  const d = v.run?.log_dir ?? v.job?.plan ?? v.job?.run
  return d ? runFilesRel(d) : null
}

export function RunsSection({ planId, runs, jobs, configured, jobsError, admin, busy, act, fmtBytes, refreshing }: {
  planId: number
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
  const views = useMemo(() => joinRuns(runs, jobs, planId), [runs, jobs, planId])
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
  const pages = Math.max(1, Math.ceil(views.length / pageSize))
  const pg = Math.min(page, pages - 1)
  const shown = views.slice(pg * pageSize, (pg + 1) * pageSize)
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set())
  const now = Math.floor(Date.now() / 1000)
  const cols = 16

  if (!views.length) return null
  return (
    <section id="runs">
      <h3>Runs ({views.length}){refreshing && <span className="dim refreshing"> · refreshing…</span>}</h3>
      {!configured && <p className="dim">Dispatch isn't configured on this deployment (no executor credentials): only recorded runs are listed, without live state.</p>}
      {jobsError && <p className="err">Batch jobs: {jobsError.message}</p>}
      <div className="runs-wrap">
        <table className="runs">
          <thead>
            <tr>
              <th /><th>run</th><th>by</th><th>mode</th><th>buckets</th>
              <th className="num"><Tooltip content="What the run's manifest step found under the plan's prefixes at its scan."><span>planned</span></Tooltip></th>
              <th className="num">deleted</th>
              <th className="num"><Tooltip content="Planned keys already gone when the run re-listed."><span>gone</span></Tooltip></th>
              <th className="num"><Tooltip content="Planned keys rewritten since the scan (a new generation): left alone."><span>overwritten</span></Tooltip></th>
              <th className="num"><Tooltip content="Directories that gained keys since the scan."><span>drift</span></Tooltip></th>
              <th>state</th><th>started</th><th className="num">elapsed</th>
              <th><Tooltip content="A real run is recoverable until then (the soft-delete / versioning window)."><span>undo by</span></Tooltip></th>
              <th>links</th><th />
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
              const secs = elapsed(v, now)
              const startedTs = r?.started_ts ?? (job?.created ? Date.parse(job.created) / 1000 : null)
              const buckets = viewBuckets(v)
              const actor = r?.actor ?? job?.by ?? ''
              const dir = dirOf(v)
              const isOpen = open.has(v.key)
              const toggle = () => setOpen(o => { const n = new Set(o); if (n.has(v.key)) n.delete(v.key); else n.add(v.key); return n })
              const failed = state === 'FAILED'
              const jobId = job?.job_id ?? v.key
              return (
                <Fragment key={v.key}>
                  <tr id={`run-${v.key.replace(/[^A-Za-z0-9_-]+/g, '-')}`} className={['run', mode, state.toLowerCase(), failed ? 'failed' : '', live ? 'live' : ''].filter(Boolean).join(' ')}>
                    <td>{r && <button type="button" className="fold" aria-expanded={isOpen} aria-label={isOpen ? 'hide run detail' : 'show run detail'} onClick={toggle}>{isOpen ? '▾' : '▸'}</button>}</td>
                    <td className="rid"><code>{(r?.run_id ?? jobId).replace(/^[a-z0-9]+-sweep-(dry|real)-/, '')}</code></td>
                    <td>{actor && <UserChip who={actor} size={16} />}</td>
                    <td>{mode === 'real' ? <span className="tag real">REAL</span> : 'dry'}</td>
                    <td className="nb">
                      {buckets.length ? buckets.map(shortBucket).join(', ') : 'all'}
                      {job?.bucket_region && job.region && job.bucket_region !== job.region && (
                        <Tooltip content={`Ran in ${job.region}, not the bucket's ${job.bucket_region} (Batch may not offer the bucket's region).`}><span className="dim"> · {job.region}</span></Tooltip>
                      )}
                    </td>
                    <td className="num nb">{plan ? <>{fmtBytes(plan.bytes)} <span className="dim">· {fmtN(plan.objects)}</span></> : <span className="dim">—</span>}</td>
                    <td className="num nb">
                      {r?.finished_ts ? (
                        <>
                          {fmtBytes(r.deleted_bytes)} <span className="dim">· {fmtN(r.deleted_objects)}</span>
                          {r.freed_bytes != null && (
                            <Tooltip content={<>What deleting this set would <b>actually</b> free, measured on the laptop: bytes it shares with a clone or hardlink outside the set (e.g. a <code>.venv</code>’s files cloned from <code>~/.cache/uv</code>) stay on disk. The size before it counts every path in full.</>}>
                              <span className="frees"> · frees <b>{fmtBytes(r.freed_bytes)}</b></span>
                            </Tooltip>
                          )}
                        </>
                      ) : live && p ? (
                        <Tooltip content={`${p.deletes.toLocaleString('en-US')}${plan ? ` of ${plan.objects.toLocaleString('en-US')}` : ''} · ${p.rate.toLocaleString('en-US')}/s · ${p.roots_done.toLocaleString('en-US')} / ${p.roots.toLocaleString('en-US')} roots`}>
                          <span className="prog"><progress max={plan?.objects || undefined} value={p.deletes} /> {fmtBytes(p.bytes)} · {fmtN(p.deletes)} · {p.rate.toLocaleString('en-US')}/s</span>
                        </Tooltip>
                      ) : live && CAPS.runFiles && !plan ? (
                        <Tooltip content="The manifest step is still streaming the scan listing; deletes start once it lands."><span className="dim">planning…</span></Tooltip>
                      ) : live && CAPS.runFiles ? (
                        <Tooltip content="No progress file yet: deletes are landing, but only the final log will say how many."><span className="dim">no progress file</span></Tooltip>
                      ) : <span className="dim">—</span>}
                    </td>
                    <td className="num">{r?.finished_ts ? fmtN(r.skipped_gone) : p ? fmtN(p.gone) : <span className="dim">—</span>}</td>
                    <td className="num">{r?.finished_ts ? fmtN(r.skipped_overwritten ?? 0) : <span className="dim">—</span>}</td>
                    <td className="num">{r?.finished_ts ? fmtN((r.drift_dirs ?? 0) + (r.ledger_drift_dirs ?? 0)) : <span className="dim">—</span>}</td>
                    <td>
                      <span className="rstate">{state.toLowerCase()}</span>
                      {failed && job?.last_event && <details className="why"><summary>why</summary><div>{job.last_event}</div></details>}
                      {ctl.undoing && <span className="tag">undoing</span>}
                      {r?.undo_state === 'full' && <span className="tag">undone</span>}
                      {r?.undo_state === 'partial' && !ctl.undoing && <Tooltip content="An undo ran (or was dispatched) but not every deleted object is live again; undo again to retry the rest."><span className="tag">partly undone</span></Tooltip>}
                      {r?.purge_state === 'done' && <span className="tag">purged</span>}
                    </td>
                    <td><TimeCell ts={startedTs ?? undefined} /></td>
                    <td className="num">{secs == null ? <span className="dim">—</span> : fmtDur(secs)}</td>
                    <td className="nb">
                      {mode === 'real' && r?.undo_deadline ? (
                        <Tooltip content={`${utc(r.undo_deadline)} UTC`}>
                          <span className={now >= r.undo_deadline ? 'dim' : undefined}>{now >= r.undo_deadline ? 'closed' : `in ${fmtDur(r.undo_deadline - now)}`}</span>
                        </Tooltip>
                      ) : <span className="dim">—</span>}
                    </td>
                    <td className="nb links">
                      {CAPS.runFiles && dir && <Link to={`/files/${dir}`}>plan</Link>}
                      {CAPS.runFiles && dir && (r?.finished_ts || live) && <> · <Link to={`/files/${dir}${logSubdir(mode)}/`}>log</Link></>}
                      {job?.logs && <>{CAPS.runFiles && dir && ' · '}<a href={job.logs} target="_blank" rel="noreferrer">Batch ↗</a></>}
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
                    <tr className="run-detail-row"><td colSpan={cols}><RunDetail run={r} live={live} dir={dir} planned={planOf(v)?.file ?? null} fmtBytes={fmtBytes} now={now} /></td></tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      {views.length > PAGE_SIZES[0] && (
        <div className="pg runs-pg">
          <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous runs">‹</button>
          <span>{pg * pageSize + 1}–{Math.min(views.length, (pg + 1) * pageSize)} of {views.length}</span>
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
function RunDetail({ run, live, dir, planned, fmtBytes, now }: {
  run: DeletionRun
  live: boolean
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
  const [allBands, setAllBands] = useState(false)
  const decisionKeys = logged ? [...DECISION_ORDER.filter(k => k in logged.decisions), ...Object.keys(logged.decisions).filter(k => !DECISION_ORDER.includes(k)).sort()] : []

  return (
    <div className="run-detail">
      <dl className="rd-totals">
        <div><dt>D1 totals</dt><dd>{fmtBytes(r.deleted_bytes)} · {fmtN(r.deleted_objects)} {r.mode === 'real' ? 'deleted' : 'would delete'} · {fmtN(r.skipped_gone)} gone · {fmtN(r.skipped_overwritten ?? 0)} overwritten · {fmtN((r.drift_dirs ?? 0) + (r.ledger_drift_dirs ?? 0))} drifted dirs</dd></div>
        <div><dt>scan</dt><dd>{r.scan ?? '—'}{r.plan_digest ? <> · items <code>{r.plan_digest}</code></> : null}</dd></div>
        {r.mode === 'real' && <div><dt>undo</dt><dd>
          {r.undo_deadline ? <>until {utc(r.undo_deadline)} UTC {now < r.undo_deadline ? <span className="dim">(in {fmtDur(r.undo_deadline - now)})</span> : <span className="dim">(closed)</span>}</> : 'no window recorded'}
          {' · '}state <b>{r.undo_state}</b>{r.purge_state && r.purge_state !== 'none' && <> · purge <b>{r.purge_state}</b></>}
        </dd></div>}
        {r.log_dir && <div><dt>run dir</dt><dd><code>{r.log_dir}</code></dd></div>}
      </dl>

      {CAPS.runFiles && (
        <div className="rd-files">
          <h4>Planned</h4>
          {planned && plan ? (
            <p>{fmtBytes(plan.bytes)} · {fmtN(plan.objects)} objects under the plan's prefixes{planned.total?.outside_bands ? <span className="dim"> · {fmtN(planned.total.outside_bands.objects)} listed keys outside them</span> : null}</p>
          ) : <p className="dim">{live ? 'the manifest step hasn\'t written its summary yet' : 'no plan summary'}</p>}
          <h4>Logged decisions</h4>
          {log && logged ? (
            <div className="runs-wrap">
              <table className="runs rd-log">
                <thead><tr><th>bucket</th>{decisionKeys.map(k => <th key={k} className="num">{k.replace(/_/g, ' ')}</th>)}<th className="num">bytes</th><th className="num">drifted dirs</th><th className="num">failed dirs</th><th>notes</th></tr></thead>
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
          ) : <p className="dim">{run.finished_ts ? 'no decision summary in the run dir' : 'written when the run ends'}</p>}
        </div>
      )}

      {checks.length > 0 && (
        <>
          <h4>Checks</h4>
          <ul className="rd-checks">
            {checks.map(c => (
              <li key={c.label} className={c.ok ? 'ok' : 'off'}>
                {c.ok ? '✓' : '≠'} {c.label}: {fmtN(c.expected)}{!c.ok && <> vs {fmtN(c.actual)} <span className="dim">({c.actual > c.expected ? '+' : ''}{fmtN(c.actual - c.expected)})</span></>}
              </li>
            ))}
          </ul>
          {checks.some(c => !c.ok && c.label.startsWith('planned')) && <p className="dim">A planned/decided gap is keys in drifted dirs the run skipped, or roots a stop left for a re-run.</p>}
        </>
      )}

      <h4>Bands ({bands.length})</h4>
      {detail.isLoading ? <p className="dim">loading…</p> : detail.error ? <p className="err">{detail.error.message}</p> : !bands.length ? <p className="dim">{run.finished_ts ? 'no band rows' : 'recorded when the run ends'}</p> : (
        <div className="runs-wrap">
          <table className="runs rd-bands">
            <thead><tr><th>prefix</th><th className="num">bytes</th><th className="num">objects</th><th className="num">gone</th><th className="num">overwritten</th><th className="num">drift new</th><th className="num">undone</th></tr></thead>
            <tbody>
              {(allBands ? bands : bands.slice(0, 25)).map(b => (
                <tr key={b.prefix}>
                  <td><code>{b.prefix}</code></td>
                  <td className="num">{fmtBytes(b.bytes)}</td><td className="num">{fmtN(b.objects)}</td><td className="num">{fmtN(b.gone)}</td>
                  <td className="num">{fmtN(b.overwritten)}</td><td className="num">{fmtN(b.drift_new_objects)}</td><td className="num">{fmtN(b.undone_objects)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {bands.length > 25 && <button type="button" className="more" onClick={() => setAllBands(a => !a)}>{allBands ? 'top 25' : `all ${bands.length}`}</button>}
        </div>
      )}
    </div>
  )
}
