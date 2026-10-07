// The `sweep` executor (gcs's bridge; `_lib/executor.ts` is the seam,
// specs/done/staged-slack.md), and its pure parts: the run dir, the `-b` cut a plan
// allows, and the executor script over the staged set (`sweep manifest
// --plan`: specs/staged-delete.md).
//
// The plan's items are the delete set: they are snapshotted into `plan.json`
// in the run dir (a gcs plan MAY span buckets), and the submitted job runs the
// executor image with its entrypoint overridden. Dry runs use `manifest`
// then `execute`; real runs use parallel XML `execute-reviewed`, deleting
// only the matching DR's exact generations after complete log preflight and
// ≥7d soft-delete/permission checks. Both record D1 runs and bands; `plan_id`
// comes from plan.json, with no row inserted here. The `-b` cut is the plan's
// buckets (∩ `buckets`, when given). The run's item digest rides in the job
// env (`PLAN_DIGEST`); `_lib/sweepReflect.ts` copies it onto the run's row once
// the executor has recorded it finished. `PLAN_ID` rides along too, so the
// jobs list places a job on its plan before (or without) a run row.
//
// `sweepJobSpec` is the Batch spec every gcs executor job shares: a run here,
// an undo in `_lib/sweepUndo.ts` (specs/staged-runs.md).
//
// Auth to GCP: `_lib/gcp.ts` (the `GCP_SA_KEY` Pages secret — a dedicated SA
// that can submit Batch jobs, act as the job SA, and write the plan.json into
// the data bucket).
import type { D1Database } from '@cloudflare/workers-types'
import { type BatchConfig, batchConfig, notConfigured } from './batchConfig.js'
import { type DispatchErr, type DispatchReq, type ExecEnv, type Executor, type Prepared, refuse } from './dispatch.js'
import { batchJobsUrl, batchRegionFor, gcpToken } from './gcp.js'
import { bucketOf, NO_SHAPE, type PlanBucketsSnapshot, prefixShape, snapshotPlanBuckets } from './plans.js'
import { listSweepJobs, reflectSweepRuns } from './sweepReflect.js'
import { DEFAULT_SWEEP_MACHINE, SWEEP_MACHINES, type SweepMachine } from './sweepMachines.js'

/** A run's dir in the data bucket (`DATA_BUCKET`). */
export const runDir = (cfg: Pick<BatchConfig, 'dataBucket'>, jobId: string): string => `gs://${cfg.dataBucket}/sweep/runs/${jobId}`
export const planJsonPath = (cfg: Pick<BatchConfig, 'dataBucket'>, jobId: string): string => `${runDir(cfg, jobId)}/plan.json`
/** `plan.json`'s object name in the data bucket (the JSON upload API's `name`). */
export const planJsonObject = (jobId: string): string => `sweep/runs/${jobId}/plan.json`

/** The buckets a plan-sourced run touches: the plan's, cut to `requested`
 * when the body names any. Empty = the request names none of the plan's. */
export const bucketCut = (planBuckets: readonly string[], requested: readonly string[]): string[] =>
  requested.length ? planBuckets.filter(b => requested.includes(b)) : [...planBuckets]

export interface SweepScript {
  cfg: Pick<BatchConfig, 'dataBucket'>
  mode: 'dry' | 'real'
  jobId: string
  buckets: readonly string[]
  /** The run's plan.json — the staged set the manifest reads. */
  plan: string
  /** A finished, matching DR selected by the dispatch gate. */
  reviewed?: string
  pacing?: 'guided' | 'adaptive'
}

/** The Batch container's bash: DR manifest/execute or parallel reviewed XML.
 * On exit (success or failure) it pings the site's `/api/sweep/jobs`
 * with the job's read grant, so the finished run is reflected — and its
 * result posted to the plan's Slack thread — without anyone polling. */
export const sweepScript = ({ cfg, mode, jobId, buckets, plan, reviewed, pacing = 'guided' }: SweepScript): string => {
  if (mode === 'real' && !reviewed) throw new Error('real dispatch requires its reviewed DR manifest')
  if (pacing !== 'guided' && pacing !== 'adaptive') throw new Error('invalid sweep pacing mode')
  const run = runDir(cfg, jobId)
  const bflags = buckets.map(b => `-b ${b}`).join(' ')
  return [
    'set -euo pipefail',
    EXIT_TRAP,
    ...(mode === 'real' ? [
      `dt-cloud sweep execute-reviewed -e xml -j 64 -B ${Math.max(1, Math.min(6, buckets.length))} -c ${pacing} -r 8000 ${bflags} -p "${plan}" -o "${run}" -w /work/reviewed --for-real "${reviewed}"`,
    ] : [
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "${plan}" ${bflags} -o "${run}"`,
      `dt-cloud sweep execute ${bflags} "${run}"`,
    ]),
  ].join('\n')
}

/** The trap every gcs executor job sets: on exit (success or failure) ping
 * the site's `/api/sweep/jobs` with the job's read grant. */
const EXIT_TRAP = `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/sweep/jobs" || true' EXIT`

/** A gcs run id as the executor records it (`sweep_exec.run_id_for`):
 * `<scan>-p<plan_id>/<utc stamp>`. */
export const RUN_ID_RE = /^\d{4}-\d{2}-\d{2}-p\d+\/\d{8}T\d{6}Z$/

/** The undo job's bash: `sweep undo` looks the run up in D1, refuses past
 * its deadline, restores exactly the generations its `deleted/` log names,
 * and records the outcome (`undo_state`, `deletion_bands.undone_objects`). */
export const undoScript = (): string => [
  'set -euo pipefail',
  EXIT_TRAP,
  'dt-cloud sweep undo "$TARGET_RUN"',
].join('\n')

export interface SweepJobSpec {
  cfg: Pick<BatchConfig, 'project' | 'image' | 'cfAccountId' | 'dataBucket' | 'd1DbId' | 'd1DbName'>
  /** The service account the job runs as (`JOB_SA`). */
  jobSa: string
  region: string
  script: string
  /** Recorded as the run's (or the undo's) `actor`: `$USER`. */
  actor: string
  siteUrl: string
  /** Per-job variables, ahead of the shared ones. */
  env: Record<string, string>
  machine?: SweepMachine
}

/** The Batch job spec every gcs executor job shares (a sweep run, an undo):
 * the executor image running `bash -c <script>` as the job account, in the
 * region of its buckets, with the D1 + site credentials from Secret Manager. */
export const sweepJobSpec = ({ cfg, jobSa, region, script, actor, siteUrl, env, machine = DEFAULT_SWEEP_MACHINE }: SweepJobSpec): unknown => {
  const SECRET = (name: string) => `projects/${cfg.project}/secrets/${name}/versions/latest`
  return {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{ container: { imageUri: cfg.image, entrypoint: '/bin/bash', commands: ['-c', script] } }],
        computeResource: SWEEP_MACHINES[machine],
        maxRetryCount: 0,
        // 72 h: the 35M-object bucket needs ~10 h of deletes at the bucket's
        // write ceiling on top of its listing; 4 h (the old cap) fit only east5.
        maxRunDuration: '259200s',
        environment: {
          variables: {
            ...env,
            // `sweep execute` / `sweep undo` record their `actor` from $USER
            USER: actor,
            CLOUDFLARE_ACCOUNT_ID: cfg.cfAccountId,
            // `sweep manifest`'s listing root (`gs://$DATA_BUCKET`): dt-cloud
            // has no default bucket (specs/oa-decoupling.md steps 9–10)
            DATA_BUCKET: cfg.dataBucket,
            // where `sweep execute` / `sweep undo` record the run (no default D1)
            D1_DB_ID: cfg.d1DbId,
            D1_DB_NAME: cfg.d1DbName,
            SITE_URL: siteUrl,
          },
          secretVariables: {
            SITE_TOKEN: SECRET('gcs-sheet-sync-token'),
            CLOUDFLARE_API_TOKEN: SECRET('cf-pages-token'),
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: machine, bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
      serviceAccount: { email: jobSa },
      location: { allowedLocations: [`regions/${region}`] },
    },
    logsPolicy: { destination: 'CLOUD_LOGGING' },
  }
}

/** Submit a gcs executor job in `region`; null on success, else the refusal
 * (500, not 502: Cloudflare swaps an origin 502 for its own branded error
 * page, which threw away this detail on the 2026-09-11 17:20Z dispatch). */
export async function submitSweepJob(cfg: BatchConfig, token: string, region: string, jobId: string, spec: unknown): Promise<DispatchErr | null> {
  const r = await fetch(
    `${batchJobsUrl(cfg, region)}?job_id=${jobId}`,
    { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(spec) },
  )
  if (r.ok) return null
  const text = await r.text()
  let out: unknown = {}
  try { out = JSON.parse(text) } catch { out = { body: text.slice(0, 1000) } }
  console.error('batch submit failed', r.status, text.slice(0, 2000))
  return refuse(500, `batch submit failed (${r.status})`, { status: r.status, detail: out })
}

/** UTC stamp `YYYYMMDD-HHMMSS` for a job id. */
export const jobStampOf = (d: Date): string => d.toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '-').toLowerCase()

async function prepare(env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | ReturnType<typeof refuse>> {
  if (!env.GCP_SA_KEY) return refuse(503, 'dispatch not configured (GCP_SA_KEY secret missing)')
  if (!env.JOB_SA) return refuse(503, 'dispatch not configured (JOB_SA var missing)')
  const jobSa = env.JOB_SA
  const pacing = env.SWEEP_PACING ?? 'guided'
  if (pacing !== 'guided' && pacing !== 'adaptive') return refuse(503, 'dispatch not configured (SWEEP_PACING must be guided or adaptive)')
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE', 'CF_ACCOUNT_ID', 'D1_DB_ID'])
  if ('missing' in cfg) return refuse(503, notConfigured('dispatch', cfg.missing))
  const shape = prefixShape(env)
  if (!shape) return refuse(503, `dispatch ${NO_SHAPE}`)
  const requested = req.buckets ?? []
  // A cut names only scanned buckets (`STORE_BUCKETS`).
  if (requested.some(b => !shape.buckets.includes(b))) return refuse(400, 'bad bucket name')
  const snapshot: PlanBucketsSnapshot | null = await snapshotPlanBuckets(db, req.planId, shape)
  if (!snapshot) return refuse(404, 'no such plan')
  if (!snapshot.sweep.length) return refuse(400, 'plan has no items to sweep')
  const buckets = bucketCut(snapshot.buckets, requested)
  if (!buckets.length) return refuse(400, 'buckets name none of the plan\'s', { plan_buckets: snapshot.buckets })
  // The run acts on the items in its cut: that set is what the digest names.
  const prefixes = snapshot.sweep.filter(p => buckets.includes(bucketOf(p, shape.buckets)))

  const launch: Prepared['launch'] = async (date, digest, reviewed) => {
    const reviewedPrefix = `gs://${cfg.dataBucket}/sweep/runs/`
    if (req.mode === 'real' && (!reviewed?.log_dir || !reviewed.log_dir.startsWith(reviewedPrefix) || !/^gcs-sweep-dry-[0-9]{8}-[0-9]{6}z$/.test(reviewed.log_dir.slice(reviewedPrefix.length)))) {
      return refuse(409, 'not deleting: matching dry-run has no reusable manifest directory')
    }
    const region = batchRegionFor(cfg, buckets)
    const jobId = `gcs-sweep-${req.mode}-${jobStampOf(new Date())}z`
    const plan = runDir(cfg, jobId)
    const script = sweepScript({ cfg, mode: req.mode, jobId, buckets, plan: planJsonPath(cfg, jobId), reviewed: reviewed?.log_dir ?? undefined, pacing })
    const spec = sweepJobSpec({
      cfg, jobSa, region, script, actor: req.actor, siteUrl: req.siteUrl, machine: req.machine,
      env: {
        SWEEP_DATE: date,
        // the plan the job runs: `/api/sweep/jobs` places a job whose run row
        // isn't recorded yet (or never was) on its plan
        PLAN_ID: String(req.planId),
        // the run's item digest, for `sweepReflect` (the executor ignores it)
        PLAN_DIGEST: digest,
      },
    })

    const token = await gcpToken(env.GCP_SA_KEY!)
    // Drop plan.json into the run dir; the executor reads it back over gs://.
    const up = await fetch(
      `https://storage.googleapis.com/upload/storage/v1/b/${cfg.dataBucket}/o?uploadType=media&name=${encodeURIComponent(planJsonObject(jobId))}`,
      { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(snapshot) },
    )
    if (!up.ok) return refuse(500, 'plan.json write failed', { status: up.status, detail: (await up.text()).slice(0, 300) })
    const sub = await submitSweepJob(cfg, token, region, jobId, spec)
    if (sub) return sub
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
    const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET'])
    if ('missing' in cfg) return []
    return reflectSweepRuns(cfg, db, await listSweepJobs(cfg, await gcpToken(env.GCP_SA_KEY)))
  },
}
