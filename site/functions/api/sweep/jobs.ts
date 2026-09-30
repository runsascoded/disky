// GET /api/sweep/jobs — the recent `gcs-sweep-*` Batch jobs with their live
// state, so the console can show a dispatch from the moment it is submitted
// (the executor only writes `deletion_runs` once its manifest step is done,
// which can be an hour of listing). Read via the same dispatch SA as
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
import { BUCKET_REGION, GCP_PROJECT, gcpToken } from '../../_lib/gcp.js'
import { announceFinished } from '../../_lib/stagedSlack.js'
import { isSweepJob, jobIdOf, listSweepJobs, reflectSweepRuns } from '../../_lib/sweepReflect.js'

type Env = AuthEnv & ExecEnv

export interface SweepJob {
  job_id: string
  mode: 'dry' | 'real'
  state: string
  created: string
  updated: string | null
  run_secs: number | null
  by: string | null
  date: string | null
  /** The `-b` cut the job was dispatched with (empty = every bucket). */
  buckets: string[]
  /** The Batch region it runs in (its bucket's, for a one-bucket cut). */
  region: string
  /** The bucket's own region, for a one-bucket cut (null: several buckets). */
  bucket_region: string | null
  plan: string
  last_event: string | null
  logs: string
}

export const onRequestGet = async (ctx: { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ jobs: [], configured: false })
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  // Jobs live in their bucket's region: list every region a sweep can be
  // dispatched to and merge, newest first.
  const jobs = await listSweepJobs(token).catch(e => e as Error)
  if (jobs instanceof Error) { console.error('batch list failed', jobs.message); return json({ error: jobs.message }, 500) }

  const db = ctx.env.DB
  if (db) {
    const finished = await reflectSweepRuns(db, jobs)
    if (finished.length) {
      const p = announceFinished(ctx.env, db, finished, new URL(ctx.request.url).origin)
      if (ctx.waitUntil) ctx.waitUntil(p)
      else await p
    }
  }

  const out: SweepJob[] = jobs
    .filter(isSweepJob)
    .slice(0, 20)
    .map(j => {
      const job_id = jobIdOf(j)
      const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
      const script = j.taskGroups?.[0]?.taskSpec?.runnables?.[0]?.container?.commands?.join(' ') ?? ''
      const buckets = [...new Set([...script.matchAll(/(?:^|\s)-b\s+(marin-[a-z0-9-]+)/g)].map(m => m[1]))].sort()
      const ev = j.status?.statusEvents ?? []
      const last = ev.length ? ev[ev.length - 1] : null
      const dur = j.status?.runDuration
      return {
        job_id,
        mode: job_id.startsWith('gcs-sweep-real-') ? 'real' : 'dry',
        state: j.status?.state ?? 'UNKNOWN',
        created: j.createTime,
        updated: j.updateTime ?? null,
        run_secs: dur ? Number(dur.replace(/s$/, '')) : null,
        by: vars.USER ?? null,
        date: vars.SWEEP_DATE ?? null,
        buckets,
        region: j.region,
        bucket_region: buckets.length === 1 ? BUCKET_REGION[buckets[0]] ?? null : null,
        plan: `gs://oa-gcs-usage-dvx/sweep/runs/${job_id}`,
        last_event: last?.description ?? null,
        logs: `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`labels.job_uid="${j.uid}"`)}?project=${GCP_PROJECT}`,
      }
    })
  return json({ jobs: out, configured: true }, 200, { 'cache-control': 'private, max-age=10' })
}
