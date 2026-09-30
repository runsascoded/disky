// The `sweep` executor (gcs's bridge; `_lib/executor.ts` is the seam,
// specs/done/staged-slack.md), and its pure parts: the run dir, the `-b` cut a plan
// allows, and the executor script over the staged set (`sweep manifest
// --plan`: specs/staged-delete.md).
//
// The plan's items are the delete set: they are snapshotted into `plan.json`
// in the run dir (a gcs plan MAY span buckets), and the submitted job runs the
// daily-snapshot image with the entrypoint overridden to `sweep manifest
// --plan` followed by `sweep execute` — which re-lists, generation-matches,
// records to D1 (`deletion_runs`/`deletion_bands`; `plan_id` comes from the
// plan.json — no row is inserted here), and for `real` requires ≥7d soft
// delete on every bucket before deleting anything. The `-b` cut is the plan's
// buckets (∩ `buckets`, when given). The run's item digest rides in the job
// env (`PLAN_DIGEST`); `_lib/sweepReflect.ts` copies it onto the run's row once
// the executor has recorded it finished.
//
// Auth to GCP: `_lib/gcp.ts` (the `GCP_SA_KEY` Pages secret — a dedicated SA
// that can submit Batch jobs, act as the job SA, and write the plan.json into
// the data bucket).
import type { D1Database } from '@cloudflare/workers-types'
import { type DispatchReq, type ExecEnv, type Executor, type Prepared, refuse } from './dispatch.js'
import { GCP_PROJECT, batchJobsUrl, batchRegionFor, gcpToken } from './gcp.js'
import { bucketOf, type PlanBucketsSnapshot, prefixShape, snapshotPlanBuckets } from './plans.js'
import { listSweepJobs, reflectSweepRuns } from './sweepReflect.js'

export const DATA_BUCKET = 'oa-gcs-usage-dvx'
export const SWEEP_RUNS = `gs://${DATA_BUCKET}/sweep/runs`
export const runDir = (jobId: string): string => `${SWEEP_RUNS}/${jobId}`
export const planJsonPath = (jobId: string): string => `${runDir(jobId)}/plan.json`
/** `plan.json`'s object name in the data bucket (the JSON upload API's `name`). */
export const planJsonObject = (jobId: string): string => `sweep/runs/${jobId}/plan.json`

/** The buckets a plan-sourced run touches: the plan's, cut to `requested`
 * when the body names any. Empty = the request names none of the plan's. */
export const bucketCut = (planBuckets: readonly string[], requested: readonly string[]): string[] =>
  requested.length ? planBuckets.filter(b => requested.includes(b)) : [...planBuckets]

export interface SweepScript {
  mode: 'dry' | 'real'
  jobId: string
  buckets: readonly string[]
  /** The run's plan.json — the staged set the manifest reads. */
  plan: string
}

/** The Batch container's bash: manifest then execute, both against the run
 * dir. On exit (success or failure) it pings the site's `/api/sweep/jobs`
 * with the job's read grant, so the finished run is reflected — and its
 * result posted to the plan's Slack thread — without anyone polling. */
export const sweepScript = ({ mode, jobId, buckets, plan }: SweepScript): string => {
  const run = runDir(jobId)
  const bflags = buckets.map(b => `-b ${b}`).join(' ')
  return [
    'set -euo pipefail',
    `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $GCS_USAGE_TOKEN" "$SITE_URL/api/sweep/jobs" || true' EXIT`,
    `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "${plan}" ${bflags} -o "${run}"`,
    `dt-cloud sweep execute ${bflags} ${mode === 'real' ? '--for-real ' : ''}"${run}"`,
  ].join('\n')
}

const IMAGE = `us-central1-docker.pkg.dev/${GCP_PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:latest`
const JOB_SA = `gcs-usage-job@${GCP_PROJECT}.iam.gserviceaccount.com`
const CF_ACCOUNT_ID = '74981a43be0de7712369306c7b19133d'
const SECRET = (name: string) => `projects/${GCP_PROJECT}/secrets/${name}/versions/latest`

async function prepare(env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | ReturnType<typeof refuse>> {
  if (!env.GCP_SA_KEY) return refuse(503, 'dispatch not configured (GCP_SA_KEY secret missing)')
  const requested = req.buckets ?? []
  if (requested.some(b => !/^marin-[a-z0-9-]+$/.test(b))) return refuse(400, 'bad bucket name')
  const shape = prefixShape(env)
  const snapshot: PlanBucketsSnapshot | null = await snapshotPlanBuckets(db, req.planId, shape)
  if (!snapshot) return refuse(404, 'no such plan')
  if (!snapshot.sweep.length) return refuse(400, 'plan has no items to sweep')
  const buckets = bucketCut(snapshot.buckets, requested)
  if (!buckets.length) return refuse(400, 'buckets name none of the plan\'s', { plan_buckets: snapshot.buckets })
  // The run acts on the items in its cut: that set is what the digest names.
  const prefixes = snapshot.sweep.filter(p => buckets.includes(bucketOf(p, shape.buckets)))

  const launch: Prepared['launch'] = async (date, digest) => {
    const region = batchRegionFor(buckets)
    const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '-').toLowerCase()
    const jobId = `gcs-sweep-${req.mode}-${ts}z`
    const plan = runDir(jobId)
    const script = sweepScript({ mode: req.mode, jobId, buckets, plan: planJsonPath(jobId) })

    const spec = {
      taskGroups: [{
        taskCount: 1,
        taskSpec: {
          runnables: [{ container: { imageUri: IMAGE, entrypoint: '/bin/bash', commands: ['-c', script] } }],
          computeResource: { cpuMilli: 8000, memoryMib: 60000 },
          maxRetryCount: 0,
          // 72 h: the 35M-object bucket needs ~10 h of deletes at the bucket's
          // write ceiling on top of its listing; 4 h (the old cap) fit only east5.
          maxRunDuration: '259200s',
          environment: {
            variables: {
              SWEEP_DATE: date,
              // `sweep execute` records the run's `actor` from $USER
              USER: req.actor,
              CLOUDFLARE_ACCOUNT_ID: CF_ACCOUNT_ID,
              // the run's item digest, for `sweepReflect` (the executor ignores it)
              PLAN_DIGEST: digest,
              SITE_URL: req.siteUrl,
            },
            secretVariables: {
              GCS_USAGE_TOKEN: SECRET('gcs-sheet-sync-token'),
              CLOUDFLARE_API_TOKEN: SECRET('cf-pages-token'),
            },
          },
        },
      }],
      allocationPolicy: {
        instances: [{ policy: { machineType: 'n2-highmem-8', bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
        serviceAccount: { email: JOB_SA },
        location: { allowedLocations: [`regions/${region}`] },
      },
      logsPolicy: { destination: 'CLOUD_LOGGING' },
    }

    const token = await gcpToken(env.GCP_SA_KEY!)
    // Drop plan.json into the run dir; the executor reads it back over gs://.
    const up = await fetch(
      `https://storage.googleapis.com/upload/storage/v1/b/${DATA_BUCKET}/o?uploadType=media&name=${encodeURIComponent(planJsonObject(jobId))}`,
      { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(snapshot) },
    )
    if (!up.ok) return refuse(500, 'plan.json write failed', { status: up.status, detail: (await up.text()).slice(0, 300) })
    const r = await fetch(
      `${batchJobsUrl(region)}?job_id=${jobId}`,
      { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(spec) },
    )
    const text = await r.text()
    let out: unknown = {}
    try { out = JSON.parse(text) } catch { out = { body: text.slice(0, 1000) } }
    // 500, not 502: Cloudflare swaps an origin 502 for its own branded error
    // page, which threw away this detail on the 2026-09-11 17:20Z dispatch.
    if (!r.ok) {
      console.error('batch submit failed', r.status, text.slice(0, 2000))
      return refuse(500, `batch submit failed (${r.status})`, { status: r.status, detail: out })
    }
    return { job_id: jobId, extra: { plan, region, buckets } }
  }
  return { prefixes, launch }
}

export const sweep: Executor = {
  dateRe: /^\d{4}-\d{2}-\d{2}$/,
  dateHint: 'YYYY-MM-DD',
  prepare,
  async refresh(env, db) {
    if (!env.GCP_SA_KEY) return []
    return reflectSweepRuns(db, await listSweepJobs(await gcpToken(env.GCP_SA_KEY)))
  },
}
