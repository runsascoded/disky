// Undo a gcs real run (specs/staged-runs.md): `POST /api/sweep/undo`
// dispatches `dt-cloud sweep undo <run_id>` as a Batch job on the dispatch's
// own spec (`sweepJobSpec`: image, job account, D1 + site secrets). The job
// restores exactly the generations the run's `deleted/` log names (GCS soft
// delete), refuses on its own past the run's deadline or where a bucket's soft
// delete is off now, and records what it restores as it goes (`undo_state`
// partial / full, `deletion_bands.undone_objects`: `sweep_exec.UndoRecord`).
// Here: the gate (the run is a finished real run inside its undo window, not
// already undone, and soft delete isn't off now on a bucket it deleted from:
// `softDeleteNow.ts`) and the submit. The submit writes nothing on the run's
// row: an undo job that is refused, or dies before it restores anything, has
// undone nothing, and only the job knows what it restored. While the job is
// live the page says so from the job itself (`runControls`' `undoing`).
import { batchConfig, notConfigured } from './batchConfig.js'
import { type DispatchErr, type ExecEnv, refuse } from './dispatch.js'
import { batchRegionFor, gcpToken } from './gcp.js'
import { auditRunControl } from './plans.js'
import { runBuckets, runUndoNow } from './softDeleteNow.js'
import { jobStampOf, RUN_ID_RE, submitSweepJob, sweepJobSpec, undoScript } from './sweepDispatch.js'
import { runPromisedNothing, type UndoUnavailable, undoUnavailableText } from '../../src/softDelete.js'

export { runBuckets }

/** The `deletion_runs` columns the undo gate reads. */
export interface UndoRunRow {
  run_id: string
  mode: string
  finished_ts: number | null
  undo_state: string
  undo_deadline: number | null
  buckets: string | null
  /** The narrowest retention measured on its buckets (absent: a D1 from before the column). */
  soft_delete_days?: number | null
  /** The minimum it ran under. */
  min_soft_delete_days?: number | null
}

/** May `row` be undone at `now`? Null = yes, else the refusal. `unavailable`: soft delete is off now on a bucket it deleted
 * from, as of the job's newest reading (`runUndoNow`; null: its buckets are on, or nothing says, and then the job, which
 * asks GCS itself, is the judge). The run's own record is asked first, and left as it is. */
export function undoGate(row: UndoRunRow | null, now: number, unavailable: UndoUnavailable | null = null): DispatchErr | null {
  if (!row) return refuse(404, 'no such run')
  if (row.mode !== 'real') return refuse(400, 'only real runs can be undone (a dry run deleted nothing)')
  if (row.finished_ts == null) return refuse(409, 'the run is still in progress; stop it first, undo once it has recorded its end')
  if (row.undo_state === 'full') return refuse(409, 'already undone')
  if (row.soft_delete_days === 0) return refuse(409, 'no undo: a bucket in this run had soft delete off, so what it deleted there is gone for good')
  if (runPromisedNothing(row)) return refuse(409, 'no undo promised: this run required no soft delete and recorded no retention, so treat its deletes as permanent')
  if (row.undo_deadline == null) return refuse(409, 'the run recorded no undo window')
  if (now >= row.undo_deadline) return refuse(409, 'undo window closed (the soft-delete retention has passed)')
  // The reading is as old as the last scan; the job asks GCS itself, and so does the CLI by hand.
  if (unavailable) return refuse(409, `${undoUnavailableText(unavailable)}; if that has changed since, \`dt-cloud sweep undo\` asks GCS itself`)
  return null
}

export interface UndoOk { ok: true; job_id: string; target: string; region: string }

export async function undoSweepRun(
  env: ExecEnv,
  runId: string,
  actor: string,
  siteUrl: string,
  now: Date = new Date(),
  { bulk = false }: { bulk?: boolean } = {},
): Promise<UndoOk | DispatchErr> {
  if (!env.DB) return refuse(503, 'undo not configured (no D1 binding)')
  if (!env.GCP_SA_KEY) return refuse(503, 'undo not configured (GCP_SA_KEY secret missing)')
  if (!env.JOB_SA) return refuse(503, 'undo not configured (JOB_SA var missing)')
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE', 'CF_ACCOUNT_ID', 'D1_DB_ID'])
  if ('missing' in cfg) return refuse(503, notConfigured('undo', cfg.missing))
  if (!RUN_ID_RE.test(runId)) return refuse(400, 'bad run_id')
  const db = env.DB
  const row = await db.prepare(
    'SELECT * FROM deletion_runs WHERE run_id = ?',
  ).bind(runId).first<UndoRunRow>()
  const nowS = Math.floor(now.getTime() / 1000)
  // The run's own record first; the reading is fetched only for a run that record would let through.
  const no = undoGate(row, nowS) ?? undoGate(row, nowS, (await runUndoNow(env, db, row!, nowS)).unavailable)
  if (no) return no

  const region = batchRegionFor(cfg, await runBuckets(db, row!))
  const jobId = `gcs-undo-${jobStampOf(now)}z`
  const spec = sweepJobSpec({
    cfg, jobSa: env.JOB_SA, region, script: undoScript({ bulk }), actor, siteUrl,
    env: { OP: 'undo', TARGET_RUN: runId }, component: 'sweep-undo',
  })
  const token = await gcpToken(env.GCP_SA_KEY)
  const failed = await submitSweepJob(cfg, token, region, jobId, spec)
  if (failed) return failed
  await auditRunControl(db, 'undo', runId, actor, jobId)
  return { ok: true, job_id: jobId, target: runId, region }
}
