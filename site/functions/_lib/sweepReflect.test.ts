import { describe, expect, it, vi } from 'vitest'
import { outcomeReaderOf, UNREAD } from './sweepOutcome'
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
    describe('a finished job\'s outcome, for its Slack message', () => {
      const finishedRow = async (id: string) => {
        const { db } = await sqliteD1()
        await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@staff.example.test', 1)").run()
        await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
          VALUES ('r', ?, '2026-09-28', 0, 0, 'ann@staff.example.test', 'real', 100, 500, ?, 1)`).bind(runDir(CFG, id), runDir(CFG, id)).run()
        return db
      }
      const id = 'gcs-sweep-real-ok'
      it('reads it from the job\'s own run dir', async () => {
        const db = await finishedRow(id)
        const read: string[] = []
        const outcome = async (jobId: string) => { read.push(jobId); return { attempted: true, skipped: { bytes: 2, objects: 1 }, notSwept: ['b1'] } }
        expect(await reflectSweepRuns(CFG, db, [job(id, 'SUCCEEDED', 'D')], SOON, undefined, RUN_DIR, outcome)).toEqual([
          { run_id: 'r', ok: true, outcome: { attempted: true, skipped: { bytes: 2, objects: 1 }, notSwept: ['b1'] } },
        ])
        expect(read).toEqual([id])
      })
      it('none to read: the run announces as it did', async () => {
        const db = await finishedRow(id)
        expect(await reflectSweepRuns(CFG, db, [job(id, 'SUCCEEDED', 'D')], SOON, undefined, RUN_DIR, async () => null)).toEqual([{ run_id: 'r', ok: true }])
      })
      it('a failed job has no totals to qualify', async () => {
        const db = await finishedRow(id)
        const outcome = async () => { throw new Error('a failed job has no outcome to read') }
        expect(await reflectSweepRuns(CFG, db, [job(id, 'FAILED', 'D')], SOON, undefined, RUN_DIR, outcome)).toEqual([{ run_id: 'r', ok: false }])
      })
      it('a store that fails loses the skip line, not the run\'s announcement', async () => {
        const db = await finishedRow(id)
        const log = vi.spyOn(console, 'log').mockImplementation(() => {})
        const get = async () => { throw new Error('store down') }
        expect(await reflectSweepRuns(CFG, db, [job(id, 'SUCCEEDED', 'D')], SOON, undefined, RUN_DIR, outcomeReaderOf(get))).toEqual([{ run_id: 'r', ok: true, outcome: UNREAD }])
        log.mockRestore()
      })
    })
    describe('a real run with a start row and no end record closes from what it wrote', () => {
      const id = 'gcs-sweep-real-killed'
      const opened = async (softDeleteDays: number | null = null, minSoftDeleteDays: number | null = null) => {
        const { db, raw } = await sqliteD1()
        raw.exec("INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1)")
        await db.prepare(`INSERT INTO deletion_runs (run_id,plan_id,${RUN_DIR},scan,head,exec_head,actor,mode,started_ts,log_dir,buckets,soft_delete_days,min_soft_delete_days)
          VALUES ('r',1,'manifest','scan',0,0,'actor','real',100,?,'b1,b2',?,?)`).bind(runDir(CFG, id), softDeleteDays, minSoftDeleteDays).run()
        const row = () => raw.prepare('SELECT finished_ts,deleted_objects,deleted_bytes,skipped_gone,skipped_overwritten,drift_dirs,undo_deadline,plan_digest FROM deletion_runs').all()
        return { db, row }
      }
      const TOTALS = { deleted_objects: 7, deleted_bytes: 70, skipped_gone: 2, skipped_overwritten: 1, drift_dirs: 0 }
      const ZEROS = { deleted_objects: 0, deleted_bytes: 0, skipped_gone: 0, skipped_overwritten: 0, drift_dirs: 0 }
      const WEEK = 7 * 86400
      const ATTEMPTED = async () => ({ attempted: true, skipped: null, notSwept: [] })
      it.each<[string, number | null, number | null, number | null]>([
        ['soft delete off: no window', 0, 0, null],
        ['the retention it measured', 14, 7, SOON + 14 * 86400],
        ['a row from before the columns: its image required 7 days', null, null, SOON + 7 * 86400],
        // `sweep execute` measures bucket by bucket, so its start row holds only what it required.
        ['nothing measured, 3 days required: those', null, 3, SOON + 3 * 86400],
        ['nothing measured, 14 days required: those', null, 14, SOON + 14 * 86400],
        ['nothing measured, nothing required: no window', null, 0, null],
      ])('the undo deadline: %s', async (_, softDeleteDays, minSoftDeleteDays, deadline) => {
        const { db, row } = await opened(softDeleteDays, minSoftDeleteDays)
        const killed = { ...job(id, 'FAILED', 'D'), updateTime: new Date(SOON * 1000).toISOString() }
        await reflectSweepRuns(CFG, db, [killed], SOON, undefined, RUN_DIR, ATTEMPTED, async () => ({ ...TOTALS, exact: false }))
        expect(row()).toEqual([{ finished_ts: SOON, ...TOTALS, undo_deadline: deadline, plan_digest: '' }])
      })
      it('its progress sums: a lower bound, with an undo window from the job\'s end', async () => {
        const { db, row } = await opened()
        const asked: unknown[] = []
        const totals = async (...args: unknown[]) => { asked.push(args); return { ...TOTALS, exact: false } }
        const ended = SOON - 600
        const killed = { ...job(id, 'FAILED', 'D'), updateTime: new Date(ended * 1000).toISOString() }
        const reason = async () => 'exit 137'
        expect(await reflectSweepRuns(CFG, db, [killed], SOON, reason, RUN_DIR, ATTEMPTED, totals)).toEqual([
          { run_id: 'r', ok: false, reason: 'exit 137', outcome: { attempted: true, skipped: null, notSwept: [], totals: 'lower-bound' } },
        ])
        expect(asked).toEqual([[id, 'b1,b2']])
        expect(row()).toEqual([{ finished_ts: ended, ...TOTALS, undo_deadline: ended + WEEK, plan_digest: '' }])
        expect(await reflectSweepRuns(CFG, db, [killed], SOON + 1, reason, RUN_DIR, ATTEMPTED, totals)).toEqual([])
        expect(asked).toEqual([[id, 'b1,b2']])
      })
      it('its `deleted-summary.json` (the final record failed on a job that succeeded): exact, and an ordinary finish', async () => {
        const { db, row } = await opened()
        expect(await reflectSweepRuns(CFG, db, [job(id, 'SUCCEEDED', 'D')], SOON, undefined, RUN_DIR, ATTEMPTED, async () => ({ ...TOTALS, exact: true }))).toEqual([
          { run_id: 'r', ok: true, outcome: { attempted: true, skipped: null, notSwept: [], totals: 'exact' } },
        ])
        expect(row()).toEqual([{ finished_ts: SOON, ...TOTALS, undo_deadline: SOON + WEEK, plan_digest: 'D' }])
      })
      it('exact totals on a job that failed: still a failure, reviewed nothing', async () => {
        const { db, row } = await opened()
        expect(await reflectSweepRuns(CFG, db, [job(id, 'FAILED', 'D')], SOON, undefined, RUN_DIR, undefined, async () => ({ ...TOTALS, exact: true }))).toEqual([
          { run_id: 'r', ok: false, outcome: { attempted: false, skipped: null, notSwept: [], totals: 'exact' } },
        ])
        expect(row()).toEqual([{ finished_ts: SOON, ...TOTALS, undo_deadline: SOON + WEEK, plan_digest: '' }])
      })
      it('a failed read leaves the row open for the next poll; past the retry window it closes, unread, undoable', async () => {
        const { db, row } = await opened()
        const killed = { ...job(id, 'FAILED', 'D'), updateTime: new Date(SOON * 1000).toISOString() }
        const unread = async () => null
        expect(await reflectSweepRuns(CFG, db, [killed], SOON + 3599, undefined, RUN_DIR, undefined, unread)).toEqual([])
        expect(row()).toEqual([{ finished_ts: null, ...ZEROS, undo_deadline: null, plan_digest: null }])
        expect(await reflectSweepRuns(CFG, db, [killed], SOON + 3600, undefined, RUN_DIR, undefined, unread)).toEqual([
          { run_id: 'r', ok: false, outcome: { attempted: false, skipped: null, notSwept: [], totals: 'unread' } },
        ])
        expect(row()).toEqual([{ finished_ts: SOON, ...ZEROS, undo_deadline: SOON + WEEK, plan_digest: '' }])
      })
      it('no reader (the store isn\'t configured): closes unread at once, as no retry would help', async () => {
        const { db, row } = await opened()
        expect(await reflectSweepRuns(CFG, db, [job(id, 'FAILED', 'D')], SOON, undefined, RUN_DIR)).toEqual([
          { run_id: 'r', ok: false, outcome: { attempted: false, skipped: null, notSwept: [], totals: 'unread' } },
        ])
        expect(row()).toEqual([{ finished_ts: SOON, ...ZEROS, undo_deadline: SOON + WEEK, plan_digest: '' }])
      })
      it('a run that recorded its end, or is still running, is never read', async () => {
        const { db } = await opened()
        const totals = async () => { throw new Error('nothing to read') }
        expect(await reflectSweepRuns(CFG, db, [job(id, 'RUNNING', 'D')], SOON, undefined, RUN_DIR, undefined, totals)).toEqual([])
        await db.prepare('UPDATE deletion_runs SET finished_ts = 500').run()
        expect(await reflectSweepRuns(CFG, db, [job(id, 'SUCCEEDED', 'D')], SOON, undefined, RUN_DIR, undefined, totals)).toEqual([{ run_id: 'r', ok: true }])
      })
    })
    it('a succeeded job has no reason to read', async () => {
      const { db } = await sqliteD1()
      await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@staff.example.test', 1)").run()
      await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
        VALUES ('r', ?, '2026-09-28', 0, 0, 'ann@staff.example.test', 'real', 100, 500, ?, 1)`).bind(runDir(CFG, 'gcs-sweep-real-ok'), runDir(CFG, 'gcs-sweep-real-ok')).run()
      const reason = async () => { throw new Error('a succeeded job has no failure to read') }
      expect(await reflectSweepRuns(CFG, db, [job('gcs-sweep-real-ok', 'SUCCEEDED', 'D')], SOON, reason, RUN_DIR)).toEqual([{ run_id: 'r', ok: true }])
    })
    it('copies PLAN_DIGEST onto finished rows, closes rows whose job died, leaves the rest; idempotent', async () => {
      const { db } = await sqliteD1()
      await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@staff.example.test', 1)").run()
      const row = async (runId: string, jobId: string, finished: number | null) => db.prepare(`
      INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id)
      VALUES (?, ?, '2026-09-28', 0, 0, 'ann@staff.example.test', 'dry', 100, ?, ?, 1)
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
