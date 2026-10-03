// Undo a gcs real run (specs/staged-runs.md): `POST /api/sweep/undo`
// dispatches `dt-cloud sweep undo <run_id>` as a Batch job on the dispatch's
// own spec (`sweepJobSpec`: image, job account, D1 + site secrets). The job
// restores exactly the generations the run's `deleted/` log names (GCS soft
// delete), refuses on its own past the run's deadline, and records the
// outcome (`undo_state` full / partial, `deletion_bands.undone_objects`).
// Here: the gate (the run is a finished real run inside its undo window, not
// already undone) and the submit, which marks the row `undo_state = 'partial'`
// as plan-sweep's undo does — the job's record then settles it.
import type { D1Database } from '@cloudflare/workers-types'
import { batchConfig, notConfigured } from './batchConfig.js'
import { type DispatchErr, type ExecEnv, refuse } from './dispatch.js'
import { batchRegionFor, gcpToken } from './gcp.js'
import { jobStampOf, RUN_ID_RE, submitSweepJob, sweepJobSpec, undoScript } from './sweepDispatch.js'

/** The `deletion_runs` columns the undo gate reads. */
export interface UndoRunRow {
  run_id: string
  mode: string
  finished_ts: number | null
  undo_state: string
  undo_deadline: number | null
  buckets: string | null
}

/** May `row` be undone at `now`? Null = yes, else the refusal. */
export function undoGate(row: UndoRunRow | null, now: number): DispatchErr | null {
  if (!row) return refuse(404, 'no such run')
  if (row.mode !== 'real') return refuse(400, 'only real runs can be undone (a dry run deleted nothing)')
  if (row.finished_ts == null) return refuse(409, 'the run is still in progress; stop it first, undo once it has recorded its end')
  if (row.undo_state === 'full') return refuse(409, 'already undone')
  if (row.undo_deadline == null) return refuse(409, 'the run recorded no undo window')
  if (now >= row.undo_deadline) return refuse(409, 'undo window closed (the soft-delete retention has passed)')
  return null
}

/** The buckets a run touched: its recorded `-b` cut, else the buckets its
 * bands name (`gs://<bucket>/…`), sorted. */
export async function runBuckets(db: D1Database, row: Pick<UndoRunRow, 'run_id' | 'buckets'>): Promise<string[]> {
  if (row.buckets) return row.buckets.split(',').map(b => b.trim()).filter(Boolean).sort()
  const bands = (await db.prepare('SELECT prefix FROM deletion_bands WHERE run_id = ?').bind(row.run_id).all<{ prefix: string }>()).results
  return [...new Set(bands.map(b => /^[a-z0-9]+:\/\/([^/]+)/.exec(b.prefix)?.[1]).filter((b): b is string => !!b))].sort()
}

export interface UndoOk { ok: true; job_id: string; target: string; region: string }

export async function undoSweepRun(
  env: ExecEnv,
  runId: string,
  actor: string,
  siteUrl: string,
  now: Date = new Date(),
): Promise<UndoOk | DispatchErr> {
  if (!env.DB) return refuse(503, 'undo not configured (no D1 binding)')
  if (!env.GCP_SA_KEY) return refuse(503, 'undo not configured (GCP_SA_KEY secret missing)')
  if (!env.JOB_SA) return refuse(503, 'undo not configured (JOB_SA var missing)')
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE', 'CF_ACCOUNT_ID', 'D1_DB_ID'])
  if ('missing' in cfg) return refuse(503, notConfigured('undo', cfg.missing))
  if (!RUN_ID_RE.test(runId)) return refuse(400, 'bad run_id')
  const db = env.DB
  const row = await db.prepare(
    'SELECT run_id, mode, finished_ts, undo_state, undo_deadline, buckets FROM deletion_runs WHERE run_id = ?',
  ).bind(runId).first<UndoRunRow>()
  const no = undoGate(row, Math.floor(now.getTime() / 1000))
  if (no) return no

  const region = batchRegionFor(cfg, await runBuckets(db, row!))
  const jobId = `gcs-undo-${jobStampOf(now)}z`
  const spec = sweepJobSpec({
    cfg, jobSa: env.JOB_SA, region, script: undoScript(), actor, siteUrl,
    env: { OP: 'undo', TARGET_RUN: runId },
  })
  const token = await gcpToken(env.GCP_SA_KEY)
  const failed = await submitSweepJob(cfg, token, region, jobId, spec)
  if (failed) return failed
  await db.prepare("UPDATE deletion_runs SET undo_state = 'partial' WHERE run_id = ? AND undo_state != 'full'").bind(runId).run()
  return { ok: true, job_id: jobId, target: runId, region }
}
