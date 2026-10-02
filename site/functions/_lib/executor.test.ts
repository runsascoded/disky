import type { D1Database } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import type { DispatchReq, ExecEnv, Executor } from './dispatch'
import { dispatchPlan, EXECUTORS, executorOf, type ExecutorKind, planFirstKind } from './executor'
import type { FinishedRun } from './plans'
import { sqliteD1 } from './testD1'

const A = 's3://marin-us-east-02a/a/'
const B = 's3://marin-us-east-02a/b/'
const C = 's3://marin-us-east-02a/c/'
// planDigest of each item set (pinned in plans.test.ts's scheme)
const DIGEST_AB = '2cbbbd326302e931'
const DIGEST_ABC = 'e878752b0b5cb50f'

/** A GCP-free executor over the real schema: `prepare` reads the plan's items,
 * `launch` records the run as plan-sweep does, `refresh` closes the runs
 * queued in `finish` (true = with a result, false = died without one). */
function fake(kind: ExecutorKind, db: D1Database, calls: string[]): Executor & { finish: Map<string, boolean> } {
  let n = 0
  const finish = new Map<string, boolean>()
  return {
    dateRe: EXECUTORS[kind].dateRe,
    dateHint: EXECUTORS[kind].dateHint,
    finish,
    async prepare(_env: ExecEnv, _db: D1Database, req: DispatchReq) {
      calls.push(`${kind}:prepare:${req.mode}`)
      const prefixes = (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ?').bind(req.planId).all<{ prefix: string }>()).results.map(r => r.prefix)
      return {
        prefixes,
        async launch(date: string, digest: string) {
          const jobId = `${kind}-${req.mode}-${++n}`
          calls.push(`${kind}:launch:${jobId}:${date}:${digest}`)
          await db.prepare(`
            INSERT INTO deletion_runs (run_id, plan_id, manifest, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest)
            VALUES (?, ?, 'm', ?, 0, 0, ?, ?, ?, 'l', ?)
          `).bind(jobId, req.planId, date, req.actor, req.mode, 1000 + n, digest).run()
          return { job_id: jobId, extra: { run: `gs://runs/${jobId}` } }
        },
      }
    },
    async refresh() {
      const done: FinishedRun[] = []
      for (const [run_id, ok] of finish) {
        await db.prepare(ok
          ? 'UPDATE deletion_runs SET finished_ts = 2000, deleted_bytes = 10, deleted_objects = 2 WHERE run_id = ?'
          : "UPDATE deletion_runs SET finished_ts = 2000, plan_digest = '' WHERE run_id = ?").bind(run_id).run()
        done.push({ run_id, ok })
      }
      finish.clear()
      return done
    },
  }
}

async function setup(env: Partial<ExecEnv> = {}) {
  const { db } = await sqliteD1('cw')
  await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@openathena.ai', 1)").run()
  for (const p of [A, B]) {
    await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann@openathena.ai', 1)").bind(p).run()
  }
  const calls: string[] = []
  const executors = { 'plan-sweep': fake('plan-sweep', db, calls), sweep: fake('sweep', db, calls), laptop: EXECUTORS.laptop }
  return { db, calls, executors, env: { DB: db, GCP_SA_KEY: 'test', JOB_SA: 'job@test.iam.gserviceaccount.com', ...env } as ExecEnv }
}

const req = (o: Partial<DispatchReq>): DispatchReq => ({ planId: 1, mode: 'dry', actor: 'bo@openathena.ai', siteUrl: 'https://site.test', ...o })

describe('executorOf — the deployment\'s `EXECUTOR`', () => {
  it('defaults to plan-sweep, accepts sweep, throws on anything else', () => {
    expect([executorOf({}), executorOf({ EXECUTOR: 'plan-sweep' }), executorOf({ EXECUTOR: 'sweep' }), executorOf({ EXECUTOR: 'laptop' })]).toEqual(['plan-sweep', 'plan-sweep', 'sweep', 'laptop'])
    expect(() => executorOf({ EXECUTOR: 'Sweep' })).toThrow('unknown EXECUTOR "Sweep" (expected plan-sweep | sweep | laptop)')
  })

  it('planFirstKind — the /api/plan-sweep routes dispatch to the deployment\'s plan-first executor', () => {
    expect([planFirstKind({}), planFirstKind({ EXECUTOR: 'sweep' }), planFirstKind({ EXECUTOR: 'laptop' })]).toEqual(['plan-sweep', 'plan-sweep', 'laptop'])
  })
})

describe('dispatchPlan — executor selection', () => {
  it('uses `EXECUTOR` unless the caller names its executor (an HTTP route does)', async () => {
    const s = await setup({ EXECUTOR: 'sweep' })
    await dispatchPlan(s.env, req({ date: '2026-09-28' }), undefined, s.executors)
    await dispatchPlan(s.env, req({ date: '2026-09-28T1201' }), 'plan-sweep', s.executors)
    const d = await setup()
    await dispatchPlan(d.env, req({ date: '2026-09-28T1201' }), undefined, d.executors)
    expect([s.calls, d.calls]).toEqual([
      ['sweep:prepare:dry', `sweep:launch:sweep-dry-1:2026-09-28:${DIGEST_AB}`, 'plan-sweep:prepare:dry', `plan-sweep:launch:plan-sweep-dry-1:2026-09-28T1201:${DIGEST_AB}`],
      ['plan-sweep:prepare:dry', `plan-sweep:launch:plan-sweep-dry-1:2026-09-28T1201:${DIGEST_AB}`],
    ])
  })

  it('validates the scan id per executor, needs D1, and the GCP executors need their key and job account', async () => {
    const s = await setup()
    // the key check is each GCP executor's own (`prepare`), not the seam's
    const real = { ...s.executors, sweep: EXECUTORS.sweep, 'plan-sweep': EXECUTORS['plan-sweep'] }
    expect([
      await dispatchPlan(s.env, req({ date: '2026-09-28T1201' }), 'sweep', s.executors),
      await dispatchPlan(s.env, req({}), 'plan-sweep', s.executors),
      await dispatchPlan({ ...s.env, GCP_SA_KEY: undefined }, req({ date: '2026-09-28' }), 'sweep', real),
      await dispatchPlan({ ...s.env, GCP_SA_KEY: undefined }, req({ date: '2026-09-28' }), 'plan-sweep', real),
      await dispatchPlan({ ...s.env, JOB_SA: undefined }, req({ date: '2026-09-28' }), 'sweep', real),
      await dispatchPlan({ ...s.env, JOB_SA: undefined }, req({ date: '2026-09-28' }), 'plan-sweep', real),
      await dispatchPlan({ ...s.env, DB: undefined }, req({ date: '2026-09-28' }), 'sweep', s.executors),
    ]).toEqual([
      { ok: false, status: 400, error: 'date must be a scan id (YYYY-MM-DD)' },
      { ok: false, status: 400, error: 'date required for a dry run (YYYY-MM-DD[THHMM])' },
      { ok: false, status: 503, error: 'dispatch not configured (GCP_SA_KEY secret missing)' },
      { ok: false, status: 503, error: 'dispatch not configured (GCP_SA_KEY secret missing)' },
      { ok: false, status: 503, error: 'dispatch not configured (JOB_SA var missing)' },
      { ok: false, status: 503, error: 'dispatch not configured (JOB_SA var missing)' },
      { ok: false, status: 503, error: 'plans store not configured (no D1 binding)' },
    ])
    expect(s.calls).toEqual([])
  })
})

describe('dispatchPlan — the real gate (a finished dry-run of exactly the current item set)', () => {
  it('refuses real without a dry-run, while one runs, and after it failed; allows it once one finished, on its scan', async () => {
    const s = await setup()
    const ex = s.executors['plan-sweep']
    const real = (date?: string) => dispatchPlan(s.env, req({ mode: 'real', date }), 'plan-sweep', s.executors)
    const dry = (date: string) => dispatchPlan(s.env, req({ date }), 'plan-sweep', s.executors)

    expect(await real()).toEqual({ ok: false, status: 409, error: 'not deleting: no dry-run of this plan yet' })
    await dry('2026-09-27T0000')
    expect(await real()).toEqual({ ok: false, status: 409, error: 'not deleting: a dry run is in progress (plan-sweep-dry-1)' })
    ex.finish.set('plan-sweep-dry-1', false)
    expect(await real()).toEqual({ ok: false, status: 409, error: 'not deleting: the plan changed since the last dry-run; dry-run it again' })

    await dry('2026-09-28T1201')
    ex.finish.set('plan-sweep-dry-2', true)
    expect(await real('2026-09-27T0000')).toEqual({ ok: false, status: 409, error: 'not deleting: a real run uses its dry-run\'s scan (2026-09-28T1201, plan-sweep-dry-2), not 2026-09-27T0000' })
    expect(await real()).toEqual({
      ok: true, job_id: 'plan-sweep-real-3', plan_id: 1, mode: 'real', date: '2026-09-28T1201', actor: 'bo@openathena.ai',
      digest: DIGEST_AB, extra: { run: 'gs://runs/plan-sweep-real-3' },
    })
    expect(s.calls.filter(c => c.includes(':launch:'))).toEqual([
      `plan-sweep:launch:plan-sweep-dry-1:2026-09-27T0000:${DIGEST_AB}`,
      `plan-sweep:launch:plan-sweep-dry-2:2026-09-28T1201:${DIGEST_AB}`,
      `plan-sweep:launch:plan-sweep-real-3:2026-09-28T1201:${DIGEST_AB}`,
    ])
  })

  it('refuses real when an item was staged after the dry-run, until a dry-run of the new set finishes', async () => {
    const s = await setup()
    const ex = s.executors.sweep
    const real = () => dispatchPlan(s.env, req({ mode: 'real' }), 'sweep', s.executors)
    await dispatchPlan(s.env, req({ date: '2026-09-28' }), 'sweep', s.executors)
    ex.finish.set('sweep-dry-1', true)
    await s.db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'cy@openathena.ai', 2)").bind(C).run()
    expect(await real()).toEqual({ ok: false, status: 409, error: 'not deleting: the plan changed since the last dry-run; dry-run it again' })
    await dispatchPlan(s.env, req({ date: '2026-09-29' }), 'sweep', s.executors)
    ex.finish.set('sweep-dry-2', true)
    expect(await real()).toEqual({
      ok: true, job_id: 'sweep-real-3', plan_id: 1, mode: 'real', date: '2026-09-29', actor: 'bo@openathena.ai',
      digest: DIGEST_ABC, extra: { run: 'gs://runs/sweep-real-3' },
    })
    const runs = (await s.db.prepare('SELECT run_id, mode, scan, finished_ts, plan_digest FROM deletion_runs ORDER BY started_ts').all()).results
    expect(runs).toEqual([
      { run_id: 'sweep-dry-1', mode: 'dry', scan: '2026-09-28', finished_ts: 2000, plan_digest: DIGEST_AB },
      { run_id: 'sweep-dry-2', mode: 'dry', scan: '2026-09-29', finished_ts: 2000, plan_digest: DIGEST_ABC },
      { run_id: 'sweep-real-3', mode: 'real', scan: '2026-09-29', finished_ts: null, plan_digest: DIGEST_ABC },
    ])
  })
})
