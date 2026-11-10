// The `sweep` executor's reflection (gcs; specs/done/staged-slack.md). gcs's
// executor records its own `deletion_runs` row from inside Batch (start, then
// totals at the end), so there are no totals to copy here. What the site adds:
//
//   - the run's item digest: `api/sweep/dispatch` put it in the job env
//     (`PLAN_DIGEST`); once the executor has recorded the run finished, it is
//     copied onto the row (`log_dir` = the job's run dir). That copy is the
//     "just finished" edge the Slack thread announces.
//   - a real job Batch reports terminal while its row is still open (killed, or its final
//     `record_run` failed) closes with what its run dir says (`sweepTotals.ts`) and an undo window
//     from the job's end (specs/done/sweep-killed-run-totals.md): the retention its start row measured, else
//     the minimum it required (`sweep execute` measures per bucket, so its start row has only that), none at 0,
//     7 days on a row from before either was recorded (its image required them).
//   - a dry job Batch reports terminal while its row is still open (the executor
//     died before recording the end) is closed, with the empty digest: it
//     reviewed nothing, so it never opens the real gate.
//   - a job that ended unsuccessfully with no row at all (it failed or was
//     refused before the executor recorded its start: a real run deletes nothing
//     until that row is in, and refuses if it can't be written) gets a closed
//     row, with the empty digest, so the failure reaches the runs table and the
//     plan's Slack thread like any other.
//
// Only jobs dispatched through the seam (with `PLAN_DIGEST`) are touched.
import type { D1Database } from '@cloudflare/workers-types'
import type { BatchConfig } from './batchConfig.js'
import { batchJobsUrl, batchRegions } from './gcp.js'
import { settleOpenPlan, type FinishedRun } from './plans.js'
import { runDir } from './sweepDispatch.js'
import type { FailureReader } from './sweepFailure.js'
import type { OutcomeReader } from './sweepOutcome.js'
import { DEFAULT_MIN_SOFT_DELETE_DAYS, type RunRetention } from '../../src/softDelete.js'
import type { TotalsReader } from './sweepTotals.js'

/** How long a killed run's unread totals keep its row open for a later read; then it closes, unread, so undo isn't held up. */
const CLOSE_RETRY_SECS = 3600

/** A job older than this with no row stays as it is: the first read after this shipped mustn't announce old failures. */
const NO_ROW_WINDOW_SECS = 2 * 86400

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
/** A killed real run's undo deadline, from what its start row recorded. */
const killedDeadline = (ended: number, r: RunRetention): number | null => {
  const days = r.soft_delete_days ?? r.min_soft_delete_days ?? DEFAULT_MIN_SOFT_DELETE_DAYS
  return days > 0 ? ended + days * 86400 : null
}

export const jobIdOf = (j: { name: string }): string => j.name.slice(j.name.lastIndexOf('/') + 1)

/** Reconcile completion bands that arrived after a job's first terminal reflection. */
export async function settleSweepJobs(cfg: Pick<BatchConfig, 'dataBucket'>, db: D1Database, jobs: readonly SweepBatchJob[]): Promise<string[]> {
  const dirs = [...new Set(jobs.filter(j => isSweepJob(j) && j.status?.state === 'SUCCEEDED').map(j => runDir(cfg, jobIdOf(j))))]
  const ids: string[] = []
  // Batch names and deletion run ids differ. Resolve their recorded directories, within D1's binding limit.
  for (let i = 0; i < dirs.length; i += 100) {
    const chunk = dirs.slice(i, i + 100)
    const { results } = await db.prepare(`SELECT run_id FROM deletion_runs WHERE log_dir IN (${chunk.map(() => '?').join(',')})`)
      .bind(...chunk).all<{ run_id: string }>()
    ids.push(...results.map(r => r.run_id))
  }
  return settleOpenPlan(db, ids)
}

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
 * the runs this call finished, for the caller to announce. Idempotent. `reason`
 * reads why a job failed, `outcome` what a finished job's totals don't say; `runDirColumn` is `deletion_runs`'s run-dir column, `plan`
 * in the gcs schema this executor runs on (`manifest` in `cloud`'s, for its tests). */
export async function reflectSweepRuns(
  cfg: Pick<BatchConfig, 'dataBucket'>,
  db: D1Database,
  jobs: readonly SweepBatchJob[],
  now: number = Math.floor(Date.now() / 1000),
  reason?: FailureReader,
  runDirColumn: string = 'plan',
  outcome?: OutcomeReader,
  totals?: TotalsReader,
): Promise<FinishedRun[]> {
  const done: FinishedRun[] = []
  const finish = async (run_id: string, job: SweepBatchJob, ok: boolean) => {
    const why = ok ? null : await reason?.(jobIdOf(job)) ?? null
    const result = ok ? await outcome?.(jobIdOf(job)) ?? null : null
    done.push({ run_id, ok, ...(why ? { reason: why } : {}), ...(result ? { outcome: result } : {}) })
  }
  for (const j of jobs) {
    if (!isSweepJob(j)) continue
    if (!TERMINAL.has(j.status?.state ?? '')) continue
    const digest = j.taskGroups?.[0]?.taskSpec?.environment?.variables?.PLAN_DIGEST
    if (!digest) continue
    const logDir = runDir(cfg, jobIdOf(j))
    const finished = await db.prepare(
      'UPDATE deletion_runs SET plan_digest = ? WHERE log_dir = ? AND finished_ts IS NOT NULL AND plan_digest IS NULL RETURNING run_id',
    ).bind(j.status?.state === 'SUCCEEDED' ? digest : '', logDir).all<{ run_id: string }>()
    for (const { run_id } of finished.results) await finish(run_id, j, j.status?.state === 'SUCCEEDED')
    const killed = await db.prepare(
      "SELECT * FROM deletion_runs WHERE log_dir = ? AND finished_ts IS NULL AND mode = 'real'",
    ).bind(logDir).all<{ run_id: string; buckets: string | null } & RunRetention>()
    for (const { run_id, buckets, ...retention } of killed.results) {
      const read = await totals?.(jobIdOf(j), buckets) ?? null
      const updated = j.updateTime ? Math.floor(Date.parse(j.updateTime) / 1000) : now
      const ended = Math.min(now, updated)
      if (!read && totals && now - ended < CLOSE_RETRY_SECS) continue
      const ok = !!read?.exact && j.status?.state === 'SUCCEEDED'
      const closed = await db.prepare(`
        UPDATE deletion_runs SET finished_ts = ?, plan_digest = ?, undo_deadline = ?, deleted_objects = ?, deleted_bytes = ?, skipped_gone = ?, skipped_overwritten = ?, drift_dirs = ?
        WHERE run_id = ? AND finished_ts IS NULL RETURNING run_id
      `).bind(ended, ok ? digest : '', killedDeadline(ended, retention), read?.deleted_objects ?? 0, read?.deleted_bytes ?? 0, read?.skipped_gone ?? 0, read?.skipped_overwritten ?? 0, read?.drift_dirs ?? 0, run_id).all<{ run_id: string }>()
      if (!closed.results.length) continue
      const why = ok ? null : await reason?.(jobIdOf(j)) ?? null
      const result = await outcome?.(jobIdOf(j)) ?? { attempted: false, skipped: null, notSwept: [] }
      done.push({ run_id, ok, ...(why ? { reason: why } : {}), outcome: { ...result, totals: read ? (read.exact ? 'exact' : 'lower-bound') : 'unread' } })
    }
    const died = await db.prepare(
      "UPDATE deletion_runs SET finished_ts = ?, plan_digest = '' WHERE log_dir = ? AND finished_ts IS NULL AND mode != 'real' RETURNING run_id",
    ).bind(now, logDir).all<{ run_id: string }>()
    for (const { run_id } of died.results) await finish(run_id, j, false)
    const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
    const created = Date.parse(j.createTime) / 1000
    if (j.status?.state === 'SUCCEEDED' || !vars.PLAN_ID || !vars.SWEEP_DATE || now - created > NO_ROW_WINDOW_SECS) continue
    // `plans` first: D1 enforces the foreign key, and a plan since removed has no thread to tell.
    const orphan = await db.prepare(`
      INSERT INTO deletion_runs (run_id, ${runDirColumn}, scan, head, exec_head, actor, mode, started_ts, finished_ts, plan_digest, log_dir, plan_id)
      SELECT ?, ?, ?, 0, 0, ?, ?, ?, ?, '', ?, ?
      WHERE EXISTS (SELECT 1 FROM plans WHERE id = ?) AND NOT EXISTS (SELECT 1 FROM deletion_runs WHERE log_dir = ?)
      ON CONFLICT (run_id) DO NOTHING
      RETURNING run_id
    `).bind(
      jobIdOf(j), logDir, vars.SWEEP_DATE, vars.USER ?? 'unknown', jobIdOf(j).startsWith('gcs-sweep-real-') ? 'real' : 'dry', Math.floor(created), now, logDir,
      Number(vars.PLAN_ID), Number(vars.PLAN_ID), logDir,
    ).all<{ run_id: string }>()
    for (const { run_id } of orphan.results) await finish(run_id, j, false)
  }
  return done
}
