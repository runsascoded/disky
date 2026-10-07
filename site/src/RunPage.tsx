import { useEffect } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { CAPS, useExecJobs, useRunDetail, useRunFiles } from './plans'
import { RunDetail } from './StagedRuns'
import { joinRuns, runFilesRel, runLabel, sumProgress, viewBuckets, viewLive, viewState, type PlanSummaryFile, type ProgressFile } from './runs'
import { useUnits } from './units'
import { fmtBytesPrecise } from './types'
import { RunTaskLogs } from './RunTaskLogs'

export function RunPage() {
  const id = useParams()['*'] ?? ''
  const detail = useRunDetail(id, !!id, true)
  const jobs = useExecJobs(true)
  const run = detail.data?.run
  const view = run ? joinRuns([run], jobs.data?.jobs ?? []).find(v => v.run?.run_id === run.run_id) : null
  const live = !!run && !run.finished_ts && (!view?.job || viewLive(view))
  const dir = run?.log_dir ? runFilesRel(run.log_dir) : null
  const [planQ] = useRunFiles<PlanSummaryFile>(dir ? [`${dir}plan-summary.json`] : [], live, { untilPresent: true })
  const buckets = view ? viewBuckets(view) : []
  const targets = buckets.length ? buckets : Object.keys(planQ?.data?.buckets ?? {})
  const progressQs = useRunFiles<ProgressFile>(dir ? targets.map(b => `${dir}progress/${b}.json`) : [], live)
  const progress = progressQs.flatMap(q => q.data ? [q.data] : [])
  const { units, suffixB } = useUnits()
  const fmtBytes = (b: number) => fmtBytesPrecise(b, units, suffixB)
  const { hash } = useLocation()
  useEffect(() => { if (hash && run) requestAnimationFrame(() => document.getElementById(hash.slice(1))?.scrollIntoView()) }, [hash, run, view?.job?.job_id])
  useEffect(() => { document.title = `Deletion run ${runLabel(id)}` }, [id])
  return <main className="staged-page run-page">
    <SiteNav />
    <p><Link to="/staged#runs">Staged deletions / runs</Link></p>
    <h1>{runLabel(id)}</h1>
    {detail.isLoading ? <p>Loading run…</p> : detail.error ? <p className="err">{detail.error.message}</p> : run && <>
      <p className="sub">{run.mode === 'real' ? 'Real deletion' : 'Dry run — nothing deleted'} · {view ? viewState(view).toLowerCase() : 'unknown'}{view?.job?.logs && <> · <a href={view.job.logs} target="_blank" rel="noreferrer">Batch task logs ↗</a></>}</p>
      {CAPS.runFiles && <RunDetail run={run} live={live} progress={progress.length ? sumProgress(progress) : null} dir={dir} planned={planQ?.data ?? null} fmtBytes={fmtBytes} now={Date.now() / 1000} />}
      {CAPS.runFiles && view?.job?.region && view.job.logs && <RunTaskLogs job={view.job.job_id} region={view.job.region} live={live} explorer={view.job.logs} />}
    </>}
    <SiteKbd />
  </main>
}
