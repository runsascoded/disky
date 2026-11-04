import { describe, expect, it } from 'vitest'
import { runDir } from './sweepDispatch'
import { reflectSweepRuns, type SweepBatchJob } from './sweepReflect'
import { RUN_DIR, sqliteD1 } from './testD1'

const CFG = { dataBucket: 'my-data' }

const job = (id: string, state: string, digest?: string, vars: Record<string, string> = {}): SweepBatchJob => ({
  name: `projects/p/locations/us-central1/jobs/${id}`, uid: id, createTime: '2026-09-28T12:00:00Z', region: 'us-central1',
  status: { state },
  taskGroups: [{ taskSpec: { environment: { variables: { SWEEP_DATE: '2026-09-28', ...(digest ? { PLAN_DIGEST: digest } : {}), ...vars } } } }],
})
/** 2026-09-28T12:00:00Z (the fixtures' `createTime`) + an hour. */
const SOON = Date.parse('2026-09-28T13:00:00Z') / 1000

describe('D1', () => {
  describe('reflectSweepRuns — gcs: the digest lands once the executor recorded the end', () => {
    it('does not call a failed job successful just because the executor recorded final totals', async () => {
      const { db, raw } = await sqliteD1()
      const id = 'gcs-sweep-real-failed'
      raw.exec("INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1)")
      await db.prepare(`INSERT INTO deletion_runs (run_id,plan_id,${RUN_DIR},scan,head,exec_head,actor,mode,started_ts,finished_ts,log_dir)
        VALUES ('run',1,'manifest','scan',0,0,'actor','real',100,500,?)`).bind(runDir(CFG, id)).run()
      expect(await reflectSweepRuns(CFG, db, [job(id, 'FAILED', 'review')], 900)).toEqual([{ run_id: 'run', ok: false }])
      expect(raw.prepare('SELECT finished_ts,plan_digest FROM deletion_runs').all()).toEqual([{ finished_ts: 500, plan_digest: '' }])
    })
    it('a job that failed before recording a run gets a closed row, with its reason; idempotent', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec("INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1)")
      const id = 'gcs-sweep-real-20260928-120000z'
      const jobs = [job(id, 'FAILED', 'D', { PLAN_ID: '1', USER: 'ann' })]
      const reasons: string[] = []
      const reason = async (jobId: string) => { reasons.push(jobId); return 'manifest has no generations' }
      expect(await reflectSweepRuns(CFG, db, jobs, SOON, reason, RUN_DIR)).toEqual([{ run_id: id, ok: false, reason: 'manifest has no generations' }])
      expect(reasons).toEqual([id])
      expect(raw.prepare(`SELECT run_id, ${RUN_DIR} AS dir, scan, actor, mode, started_ts, finished_ts, deleted_objects, plan_digest, log_dir, plan_id, undo_deadline FROM deletion_runs`).all()).toEqual([{
        run_id: id, dir: runDir(CFG, id), scan: '2026-09-28', actor: 'ann', mode: 'real', started_ts: SOON - 3600, finished_ts: SOON, deleted_objects: 0,
        plan_digest: '', log_dir: runDir(CFG, id), plan_id: 1, undo_deadline: null,
      }])
      expect(await reflectSweepRuns(CFG, db, jobs, SOON + 1, reason, RUN_DIR)).toEqual([])
      expect(reasons).toEqual([id])
    })
    it('a job with no row stays as it is: running, succeeded, undispatched, long past, or its plan gone', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec("INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1)")
      const on = (plan: string) => ({ PLAN_ID: plan, USER: 'ann' })
      expect(await reflectSweepRuns(CFG, db, [
        job('gcs-sweep-real-running', 'RUNNING', 'D', on('1')),
        job('gcs-sweep-real-fine', 'SUCCEEDED', 'D', on('1')),
        job('gcs-sweep-real-noplanid', 'FAILED', 'D'),
        job('gcs-sweep-real-nodigest', 'FAILED', undefined, on('1')),
        job('gcs-sweep-real-noplan', 'FAILED', 'D', on('2')),
      ], SOON, undefined, RUN_DIR)).toEqual([])
      expect(await reflectSweepRuns(CFG, db, [job('gcs-sweep-real-old', 'FAILED', 'D', on('1'))], SOON + 3 * 86400, undefined, RUN_DIR)).toEqual([])
      expect(raw.prepare('SELECT run_id FROM deletion_runs').all()).toEqual([])
    })
    it('a succeeded job has no reason to read', async () => {
      const { db } = await sqliteD1()
      await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@openathena.ai', 1)").run()
      await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
        VALUES ('r', ?, '2026-09-28', 0, 0, 'ann@openathena.ai', 'real', 100, 500, ?, 1)`).bind(runDir(CFG, 'gcs-sweep-real-ok'), runDir(CFG, 'gcs-sweep-real-ok')).run()
      const reason = async () => { throw new Error('a succeeded job has no failure to read') }
      expect(await reflectSweepRuns(CFG, db, [job('gcs-sweep-real-ok', 'SUCCEEDED', 'D')], SOON, reason, RUN_DIR)).toEqual([{ run_id: 'r', ok: true }])
    })
    it('copies PLAN_DIGEST onto finished rows, closes rows whose job died, leaves the rest; idempotent', async () => {
      const { db } = await sqliteD1()
      await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@openathena.ai', 1)").run()
      const row = async (runId: string, jobId: string, finished: number | null) => db.prepare(`
      INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
      VALUES (?, ?, '2026-09-28', 0, 0, 'ann@openathena.ai', 'dry', 100, ?, ?, 1)
    `).bind(runId, runDir(CFG, jobId), finished, runDir(CFG, jobId)).run()
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
      expect(await reflectSweepRuns(CFG, db, jobs, 900)).toEqual([
        { run_id: '2026-09-28-p1/a', ok: true },
        { run_id: '2026-09-28-p1/c', ok: false },
      ])
      expect(await reflectSweepRuns(CFG, db, jobs, 901)).toEqual([])
      expect((await db.prepare('SELECT run_id, finished_ts, plan_digest FROM deletion_runs ORDER BY run_id').all()).results).toEqual([
        { run_id: '2026-09-28-p1/a', finished_ts: 500, plan_digest: 'DA' },
        { run_id: '2026-09-28-p1/b', finished_ts: null, plan_digest: null },
        { run_id: '2026-09-28-p1/c', finished_ts: 900, plan_digest: '' },
        { run_id: '2026-09-28-p1/d', finished_ts: 500, plan_digest: null },
      ])
    })
  })
})
