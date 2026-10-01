import { describe, expect, it } from 'vitest'
import { runDir } from './sweepDispatch'
import { reflectSweepRuns, type SweepBatchJob } from './sweepReflect'
import { sqliteD1 } from './testD1'

const job = (id: string, state: string, digest?: string): SweepBatchJob => ({
  name: `projects/p/locations/us-central1/jobs/${id}`, uid: id, createTime: '2026-09-28T12:00:00Z', region: 'us-central1',
  status: { state },
  taskGroups: [{ taskSpec: { environment: { variables: { SWEEP_DATE: '2026-09-28', ...(digest ? { PLAN_DIGEST: digest } : {}) } } } }],
})

describe('reflectSweepRuns — gcs: the digest lands once the executor recorded the end', () => {
  it('copies PLAN_DIGEST onto finished rows, closes rows whose job died, leaves the rest; idempotent', async () => {
    const { db } = await sqliteD1('cw')
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@openathena.ai', 1)").run()
    const row = async (runId: string, jobId: string, finished: number | null) => db.prepare(`
      INSERT INTO deletion_runs (run_id, manifest, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
      VALUES (?, ?, '2026-09-28', 0, 0, 'ann@openathena.ai', 'dry', 100, ?, ?, 1)
    `).bind(runId, runDir(jobId), finished, runDir(jobId)).run()
    await row('2026-09-28-p1/a', 'gcs-sweep-dry-a', 500)   // executor recorded the end
    await row('2026-09-28-p1/b', 'gcs-sweep-dry-b', null)  // still running
    await row('2026-09-28-p1/c', 'gcs-sweep-dry-c', null)  // its job died
    await row('2026-09-28-p1/d', 'gcs-sweep-dry-d', 500)   // dispatched before the seam (no PLAN_DIGEST)
    const jobs = [
      job('gcs-sweep-dry-a', 'SUCCEEDED', 'DA'),
      job('gcs-sweep-dry-b', 'RUNNING', 'DB'),
      job('gcs-sweep-dry-c', 'FAILED', 'DC'),
      job('gcs-sweep-dry-d', 'SUCCEEDED'),
      job('gcs-snapshot-20260928', 'SUCCEEDED', 'DX'),
    ]
    expect(await reflectSweepRuns(db, jobs, 900)).toEqual([
      { run_id: '2026-09-28-p1/a', ok: true },
      { run_id: '2026-09-28-p1/c', ok: false },
    ])
    expect(await reflectSweepRuns(db, jobs, 901)).toEqual([])
    expect((await db.prepare('SELECT run_id, finished_ts, plan_digest FROM deletion_runs ORDER BY run_id').all()).results).toEqual([
      { run_id: '2026-09-28-p1/a', finished_ts: 500, plan_digest: 'DA' },
      { run_id: '2026-09-28-p1/b', finished_ts: null, plan_digest: null },
      { run_id: '2026-09-28-p1/c', finished_ts: 900, plan_digest: '' },
      { run_id: '2026-09-28-p1/d', finished_ts: 500, plan_digest: null },
    ])
  })
})
