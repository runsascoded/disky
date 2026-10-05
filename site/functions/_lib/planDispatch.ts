/**
 * The `plan-sweep` executor (cw's plan-first bridge; `_lib/executor.ts` is
 * the seam, specs/done/staged-slack.md). A dispatch snapshots the plan into
 * plan.json on GCS, submits the Batch job, and records an in-progress
 * `deletion_runs` row carrying the digest of the plan's item set (what a
 * later real run is gated on). One bucket per run: a plan naming several is
 * refused, not split.
 *
 * The job's exit trap pings `<site>/api/plan-sweep/jobs` with the job's read
 * grant (`cw-s3-job-grant`), so the run summary is reflected into D1
 * (`_lib/runReflect.ts`) — and its result posted to the plan's Slack thread —
 * the moment the executor finishes, not when someone next opens /staged.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { batchConfig, notConfigured } from './batchConfig.js'
import { jobStamp, runGsPath, runMountPath, secretRef, submitBatch, sweepBatchSpec } from './cwBatch.js'
import { type DispatchReq, type ExecEnv, type Executor, type Prepared, refuse } from './dispatch.js'
import { gcpToken } from './gcp.js'
import { NO_SHAPE, PlanSpansBuckets, prefixShape, snapshotPlan } from './plans.js'
import { listBatchJobs, reflectRuns } from './runReflect.js'

export const DATE_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/

async function prepare(env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | ReturnType<typeof refuse>> {
  if (!env.GCP_SA_KEY) return refuse(503, 'dispatch not configured (GCP_SA_KEY secret missing)')
  if (!env.JOB_SA) return refuse(503, 'dispatch not configured (JOB_SA var missing)')
  const jobSa = env.JOB_SA
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE', 'SWEEP_S3_ENDPOINT'])
  if ('missing' in cfg) return refuse(503, notConfigured('dispatch', cfg.missing))
  const shape = prefixShape(env)
  if (!shape) return refuse(503, `dispatch ${NO_SHAPE}`)
  let snapshot: Awaited<ReturnType<typeof snapshotPlan>>
  try {
    snapshot = await snapshotPlan(db, req.planId, shape)
  } catch (e) {
    if (e instanceof PlanSpansBuckets) return refuse(400, e.message, { buckets: e.buckets })
    throw e
  }
  if (!snapshot) return refuse(404, 'no such plan')
  if (!snapshot.sweep.length) return refuse(400, 'plan has no items to sweep')
  const plan = snapshot
  const prefixes = (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ?').bind(req.planId).all<{ prefix: string }>()).results.map(i => i.prefix)

  const launch: Prepared['launch'] = async (date, digest) => {
    const jobId = `cw-sweep-${req.mode}-${jobStamp()}z`
    const runGs = runGsPath(cfg, jobId)
    const runMnt = runMountPath(cfg, jobId)
    const token = await gcpToken(env.GCP_SA_KEY!)

    // Drop plan.json into the run dir (the executor reads it via the FUSE mount).
    const planObj = encodeURIComponent(`sweep/cw/runs/${jobId}/plan.json`)
    const up = await fetch(
      `https://storage.googleapis.com/upload/storage/v1/b/${cfg.dataBucket}/o?uploadType=media&name=${planObj}`,
      { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(plan) },
    )
    if (!up.ok) return refuse(500, 'plan.json write failed', { status: up.status, detail: (await up.text()).slice(0, 300) })

    const script = [
      'set -euo pipefail',
      // Ping the site on exit (success or failure) so it reflects the run now.
      `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/plan-sweep/jobs" || true' EXIT`,
      `RUN="${runMnt}"`,
      `dt-cloud plan-sweep manifest --plan "$RUN/plan.json" -d "$SWEEP_DATE" -o "$RUN"`,
      `dt-cloud plan-sweep execute ${req.mode === 'real' ? '--for-real ' : ''}"$RUN"`,
    ].join('\n')

    // the executor deletes from the plan's bucket (`SWEEP_BUCKET` in the job env)
    const spec = sweepBatchSpec(cfg, jobSa, script, plan.bucket, { JOB_ID: jobId, SWEEP_DATE: date, SITE_URL: req.siteUrl },
      { SITE_TOKEN: secretRef(cfg, 'cw-s3-job-grant') })
    const { ok, status, text } = await submitBatch(cfg, token, jobId, spec)
    if (!ok) {
      console.error('batch submit failed', status, text.slice(0, 2000))
      let detail: unknown = { body: text.slice(0, 1000) }
      try { detail = JSON.parse(text) } catch { /* keep raw */ }
      return refuse(500, `batch submit failed (${status})`, { status, detail })
    }

    // `head` / `exec_head` recorded a ledger position a plan-first run doesn't
    // have (it reads no ledger), so both are 0.
    await db.prepare(`
      INSERT INTO deletion_runs (run_id, plan_id, manifest, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest)
      VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)
    `).bind(jobId, req.planId, runGs, date, req.actor, req.mode, Math.floor(Date.now() / 1000), runGs, digest).run()

    return { job_id: jobId, extra: { run: runGs } }
  }
  return { prefixes, launch }
}

export const planSweep: Executor = {
  dateRe: DATE_RE,
  dateHint: 'YYYY-MM-DD[THHMM]',
  prepare,
  async refresh(env, db) {
    if (!env.GCP_SA_KEY) return []
    const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET'])
    const shape = prefixShape(env)
    if ('missing' in cfg || !shape) return []
    const token = await gcpToken(env.GCP_SA_KEY)
    const jobs = await listBatchJobs(cfg, token)
    if (!Array.isArray(jobs)) throw new Error(jobs.error)
    return reflectRuns(cfg, db, token, jobs, shape.buckets[0])
  },
}
