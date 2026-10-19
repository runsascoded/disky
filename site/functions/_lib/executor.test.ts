import type { D1Database } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import type { DispatchReq, ExecEnv, Executor } from './dispatch'
import { dispatchPlan, EXECUTORS, executorOf, type ExecutorKind, planFirstKind } from './executor'
import { type FinishedRun, planItemLines, type RunRow } from './plans'
import { RUN_DIR, sqliteD1 } from './testD1'

const A = 's3://primary-bucket/a/'
const B = 's3://primary-bucket/b/'
const C = 's3://primary-bucket/c/'
// planDigest of each item set (pinned in plans.test.ts's scheme)
const DIGEST_AB = '6c659330217cf7aa'
const DIGEST_ABC = 'aecc92b130948c2f'

/** A GCP-free executor over the real schema: `prepare` reads the plan's items,
 * `launch` records the run as plan-sweep does, `refresh` closes the runs
 * queued in `finish` (true = with a result, false = died without one). */
function fake(kind: ExecutorKind, db: D1Database, calls: string[]): Executor & { finish: Map<string, boolean>; reviewed: (RunRow | undefined)[]; acted: string[][] } {
  let n = 0
  const finish = new Map<string, boolean>()
  const reviewed: (RunRow | undefined)[] = []
  const acted: string[][] = []
  return {
    dateRe: EXECUTORS[kind].dateRe,
    dateHint: EXECUTORS[kind].dateHint,
    finish,
    reviewed,
    acted,
    async prepare(_env: ExecEnv, _db: D1Database, req: DispatchReq) {
      calls.push(`${kind}:prepare:${req.mode}`)
      // the items the run acts on, as both executors take them: the selection (`req.items`), else the plan
      const prefixes = await planItemLines(db, req.planId, req.items)
      return {
        prefixes,
        async launch(date: string, digest: string, dry?: RunRow) {
          reviewed.push(dry)
          acted.push(prefixes)
          const jobId = `${kind}-${req.mode}-${++n}`
          calls.push(`${kind}:launch:${jobId}:${date}:${digest}`)
          await db.prepare(`
            INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest)
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
  const { db } = await sqliteD1()
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
      await dispatchPlan(s.env, req({ date: '2026-09-28T12' }), 'sweep', s.executors),
      await dispatchPlan(s.env, req({}), 'plan-sweep', s.executors),
      await dispatchPlan({ ...s.env, GCP_SA_KEY: undefined }, req({ date: '2026-09-28' }), 'sweep', real),
      await dispatchPlan({ ...s.env, GCP_SA_KEY: undefined }, req({ date: '2026-09-28' }), 'plan-sweep', real),
      await dispatchPlan({ ...s.env, JOB_SA: undefined }, req({ date: '2026-09-28' }), 'sweep', real),
      await dispatchPlan({ ...s.env, JOB_SA: undefined }, req({ date: '2026-09-28' }), 'plan-sweep', real),
      await dispatchPlan({ ...s.env, DB: undefined }, req({ date: '2026-09-28' }), 'sweep', s.executors),
    ]).toEqual([
      { ok: false, status: 400, error: 'date must be a scan id (YYYY-MM-DD[THHMM])' },
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

describe('dispatchPlan — a real run (specs/delete-what-you-see.md)', () => {
  it('needs no prior dry run: a scan, or a finished dry run of exactly its set to take one (and its manifest) from; never while a run is in flight', async () => {
    const s = await setup()
    const ex = s.executors['plan-sweep']
    const real = (date?: string) => dispatchPlan(s.env, req({ mode: 'real', date }), 'plan-sweep', s.executors)
    const dry = (date: string) => dispatchPlan(s.env, req({ date }), 'plan-sweep', s.executors)

    expect(await real()).toEqual({ ok: false, status: 409, error: 'not deleting: no scan given, and no dry run of exactly this selection to take one from' })
    await dry('2026-09-27T0000')
    expect(await real('2026-09-27T0000')).toEqual({ ok: false, status: 409, error: 'not deleting: a dry run is in progress (plan-sweep-dry-1)' })
    ex.finish.set('plan-sweep-dry-1', true)
    // the dry run of this set: its scan, its manifest
    expect(await real()).toEqual({
      ok: true, job_id: 'plan-sweep-real-2', plan_id: 1, mode: 'real', date: '2026-09-27T0000', actor: 'bo@openathena.ai',
      digest: DIGEST_AB, extra: { run: 'gs://runs/plan-sweep-real-2' },
    })
    ex.finish.set('plan-sweep-real-2', true)
    // another scan: no dry run there, so its own listing
    expect((await real('2026-09-28T1201')).ok).toBe(true)
    expect([s.calls.filter(c => c.includes(':launch:')), ex.reviewed.map(r => r?.run_id ?? null)]).toEqual([[
      `plan-sweep:launch:plan-sweep-dry-1:2026-09-27T0000:${DIGEST_AB}`,
      `plan-sweep:launch:plan-sweep-real-2:2026-09-27T0000:${DIGEST_AB}`,
      `plan-sweep:launch:plan-sweep-real-3:2026-09-28T1201:${DIGEST_AB}`,
    ], [null, 'plan-sweep-dry-1', null]])
  })

  it('acts on exactly the selected rows: a dry run of every item, one unchecked, real → the other two only, listed afresh', async () => {
    const s = await setup()
    await s.db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'cy@openathena.ai', 2)").bind(C).run()
    const ex = s.executors.sweep
    await dispatchPlan(s.env, req({ date: '2026-09-28' }), 'sweep', s.executors)
    ex.finish.set('sweep-dry-1', true)
    const r = await dispatchPlan(s.env, req({ mode: 'real', date: '2026-09-28', items: [A, B] }), 'sweep', s.executors)
    expect([r, ex.acted, ex.reviewed.map(x => x?.run_id ?? null)]).toEqual([
      { ok: true, job_id: 'sweep-real-2', plan_id: 1, mode: 'real', date: '2026-09-28', actor: 'bo@openathena.ai', digest: DIGEST_AB, extra: { run: 'gs://runs/sweep-real-2' } },
      [[A, B, C], [A, B]],
      // the dry run covered C too: its manifest isn't this run's
      [null, null],
    ])
    expect((await s.db.prepare("SELECT run_id, plan_digest FROM deletion_runs WHERE mode = 'real'").all()).results).toEqual([{ run_id: 'sweep-real-2', plan_digest: DIGEST_AB }])
  })

  it('refuses a selection naming nothing, or an item the plan doesn\'t hold', async () => {
    const s = await setup()
    const go = (items: string[]) => dispatchPlan(s.env, req({ mode: 'real', date: '2026-09-28', items }), 'sweep', s.executors)
    expect([await go([]), await go([A, C])]).toEqual([
      { ok: false, status: 400, error: 'nothing selected' },
      { ok: false, status: 400, error: `not in the plan: ${C}` },
    ])
    expect(s.executors.sweep.acted).toEqual([])
  })
})
