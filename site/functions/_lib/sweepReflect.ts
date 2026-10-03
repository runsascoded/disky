// The `sweep` executor's reflection (gcs; specs/done/staged-slack.md). gcs's
// executor records its own `deletion_runs` row from inside Batch (start, then
// totals at the end), so there are no totals to copy here. What the site adds:
//
//   - the run's item digest: `api/sweep/dispatch` put it in the job env
//     (`PLAN_DIGEST`); once the executor has recorded the run finished, it is
//     copied onto the row (`log_dir` = the job's run dir). That copy is the
//     "just finished" edge the Slack thread announces.
//   - a job Batch reports terminal while its row is still open (the executor
//     died before recording the end) is closed, with the empty digest: it
//     reviewed nothing, so it never opens the real gate.
//
// Only jobs dispatched through the seam (with `PLAN_DIGEST`) are touched.
import type { D1Database } from '@cloudflare/workers-types'
import type { BatchConfig } from './batchConfig.js'
import { batchJobsUrl, batchRegions } from './gcp.js'
import type { FinishedRun } from './plans.js'
import { runDir } from './sweepDispatch.js'

export const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

export interface SweepBatchJob {
  name: string
  uid: string
  createTime: string
  updateTime?: string
  status?: { state?: string; runDuration?: string; statusEvents?: { type?: string; description?: string; eventTime?: string }[] }
  taskGroups?: { taskSpec?: { environment?: { variables?: Record<string, string> }; runnables?: { container?: { commands?: string[] } }[] } }[]
  /** The Batch region it was listed from. */
  region: string
}

export const isSweepJob = (j: { name: string }): boolean => /\/jobs\/gcs-sweep-(dry|real)-/.test(j.name)
/** An undo of a run (`api/sweep/undo`; `TARGET_RUN` in its env). */
export const isUndoJob = (j: { name: string }): boolean => /\/jobs\/gcs-undo-/.test(j.name)
export const jobIdOf = (j: { name: string }): string => j.name.slice(j.name.lastIndexOf('/') + 1)

/** Every Batch job in each region a sweep can be dispatched to, newest first.
 * Throws when a region's listing fails. */
export async function listSweepJobs(cfg: BatchConfig, token: string): Promise<SweepBatchJob[]> {
  const lists = await Promise.all(batchRegions(cfg).map(async region => {
    const r = await fetch(`${batchJobsUrl(cfg, region)}?pageSize=100&orderBy=${encodeURIComponent('create_time desc')}`, {
      headers: { authorization: `Bearer ${token}` },
    })
    if (!r.ok) throw new Error(`batch list ${region} failed: ${r.status} ${(await r.text()).slice(0, 200)}`)
    const { jobs = [] } = (await r.json()) as { jobs?: Omit<SweepBatchJob, 'region'>[] }
    return jobs.map(j => ({ ...j, region }))
  }))
  return lists.flat().sort((a, b) => (a.createTime < b.createTime ? 1 : -1))
}

/** Reflect the seam-dispatched sweep jobs into D1 (see the header); returns
 * the runs this call finished, for the caller to announce. Idempotent. */
export async function reflectSweepRuns(cfg: Pick<BatchConfig, 'dataBucket'>, db: D1Database, jobs: readonly SweepBatchJob[], now: number = Math.floor(Date.now() / 1000)): Promise<FinishedRun[]> {
  const done: FinishedRun[] = []
  for (const j of jobs) {
    if (!isSweepJob(j)) continue
    const digest = j.taskGroups?.[0]?.taskSpec?.environment?.variables?.PLAN_DIGEST
    if (!digest) continue
    const logDir = runDir(cfg, jobIdOf(j))
    const finished = await db.prepare(
      'UPDATE deletion_runs SET plan_digest = ? WHERE log_dir = ? AND finished_ts IS NOT NULL AND plan_digest IS NULL RETURNING run_id',
    ).bind(digest, logDir).all<{ run_id: string }>()
    for (const { run_id } of finished.results) done.push({ run_id, ok: true })
    if (!TERMINAL.has(j.status?.state ?? '')) continue
    const died = await db.prepare(
      "UPDATE deletion_runs SET finished_ts = ?, plan_digest = '' WHERE log_dir = ? AND finished_ts IS NULL RETURNING run_id",
    ).bind(now, logDir).all<{ run_id: string }>()
    for (const { run_id } of died.results) done.push({ run_id, ok: false })
  }
  return done
}
