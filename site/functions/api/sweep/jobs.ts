// GET /api/sweep/jobs — the recent `gcs-sweep-*` Batch jobs with their live
// state, so the console can show a dispatch from the moment it is submitted
// (the executor only writes `deletion_runs` once its manifest step is done,
// which can be an hour of listing), and the `gcs-undo-*` jobs undoing runs
// (`op: 'undo'`, `target` = the run id). Read via the same dispatch SA as
// `dispatch.ts` (`batch.jobsEditor` covers list). Any signed-in viewer of the
// console may read this; the payload holds no bucket data.
//
// Reading also reflects finished runs (`_lib/sweepReflect.ts`: the run's item
// digest, or closing a run whose job died) and posts each newly finished run's
// result to its plan's Slack thread (specs/done/staged-slack.md). The Batch job's
// exit trap calls this with the job's read grant, so that happens as the run
// ends.
import { type Env as AuthEnv, json, requireViewer } from '../../_lib/auth.js'
import type { ExecEnv } from '../../_lib/dispatch.js'
import { type BatchConfig, batchConfig, notConfigured } from '../../_lib/batchConfig.js'
import { batchLogsUrl, gcpToken } from '../../_lib/gcp.js'
import { runDir } from '../../_lib/sweepDispatch.js'
import { announceFinished } from '../../_lib/stagedSlack.js'
import { isSweepJob, isUndoJob, jobIdOf, listSweepJobs, reflectSweepRuns, type SweepBatchJob } from '../../_lib/sweepReflect.js'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { storeCreds, storeReady, storeTarget } from '../../_lib/index.js'
import { intentionalStop } from '../../_lib/sweepStop.js'

type Env = AuthEnv & ExecEnv

export interface SweepJob {
  job_id: string
  /** `sweep` = a dry / real run; `undo` = an undo of `target`. */
  op: 'sweep' | 'undo'
  mode: 'dry' | 'real'
  state: string
  created: string
  updated: string | null
  run_secs: number | null
  by: string | null
  date: string | null
  /** The plan the job was dispatched for (`PLAN_ID`; null: an undo, or a job
   *  dispatched before jobs carried it). */
  plan_id: number | null
  /** An undo's run id (`TARGET_RUN`). */
  target: string | null
  /** The `-b` cut the job was dispatched with (empty = every bucket). */
  buckets: string[]
  /** The Batch region it runs in (its bucket's, for a one-bucket cut). */
  region: string
  /** The bucket's own region, for a one-bucket cut (null: several buckets). */
  bucket_region: string | null
  plan: string
  last_event: string | null
  logs: string
  stop_requested?: boolean
}

/** One listed Batch job as the console reads it (pure). */
export function sweepJobView(cfg: Pick<BatchConfig, 'project' | 'dataBucket' | 'bucketRegions'>, j: SweepBatchJob): SweepJob {
  const job_id = jobIdOf(j)
  const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
  const script = j.taskGroups?.[0]?.taskSpec?.runnables?.[0]?.container?.commands?.join(' ') ?? ''
  const buckets = [...new Set([...script.matchAll(/(?:^|\s)-b\s+(\S+)/g)].map(m => m[1]))].sort()
  const ev = j.status?.statusEvents ?? []
  const last = ev.length ? ev[ev.length - 1] : null
  const dur = j.status?.runDuration
  const undo = isUndoJob(j)
  return {
    job_id,
    op: undo ? 'undo' : 'sweep',
    mode: undo || job_id.startsWith('gcs-sweep-real-') ? 'real' : 'dry',
    state: j.status?.state ?? 'UNKNOWN',
    created: j.createTime,
    updated: j.updateTime ?? null,
    run_secs: dur ? Number(dur.replace(/s$/, '')) : null,
    by: vars.USER ?? null,
    date: vars.SWEEP_DATE ?? null,
    plan_id: vars.PLAN_ID ? Number(vars.PLAN_ID) : null,
    target: vars.TARGET_RUN ?? null,
    buckets,
    region: j.region,
    bucket_region: buckets.length === 1 ? cfg.bucketRegions[buckets[0]] ?? null : null,
    plan: runDir(cfg, job_id),
    last_event: last?.description ?? null,
    logs: batchLogsUrl(cfg.project, j.uid, j.createTime),
  }
}

export const onRequestGet = async (ctx: { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ jobs: [], configured: false })
  const cfg = batchConfig(ctx.env, ['GCP_PROJECT', 'DATA_BUCKET'])
  if ('missing' in cfg) return json({ error: notConfigured('jobs', cfg.missing) }, 503)
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  // Jobs live in their bucket's region: list every region a sweep can be
  // dispatched to and merge, newest first.
  const jobs = await listSweepJobs(cfg, token).catch(e => e as Error)
  if (jobs instanceof Error) { console.error('batch list failed', jobs.message); return json({ error: jobs.message }, 500) }

  const db = ctx.env.DB
  if (db) {
    const finished = await reflectSweepRuns(cfg, db, jobs)
    if (finished.length) {
      const p = announceFinished(ctx.env, db, finished, new URL(ctx.request.url).origin)
      if (ctx.waitUntil) ctx.waitUntil(p)
      else await p
    }
  }

  const out = jobs.filter(j => isSweepJob(j) || isUndoJob(j)).slice(0, 30).map(j => sweepJobView(cfg, j))
  if (storeReady(ctx.env)) {
    const store = S3Store({ ...storeTarget(ctx.env), ...storeCreds(ctx.env), prefixes: ['sweep/runs/'] })
    await Promise.all(out.map(async j => {
      if (j.op === 'sweep' && await intentionalStop(j, store.get)) j.stop_requested = true
    }))
  }
  return json({ jobs: out, configured: true }, 200, { 'cache-control': 'private, max-age=10' })
}
