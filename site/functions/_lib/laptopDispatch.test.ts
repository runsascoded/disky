import type { D1Database } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import type { DispatchReq } from './dispatch'
import { dispatchPlan, EXECUTORS } from './executor'
import { AGENT, DATE_RE, laptopExecutor, laptopStatus, STALE_S } from './laptopDispatch'
import { planDigest } from './plans'
import { sqliteD1 } from './testD1'

const A = 'file:///Users/ryan/c/old-repo/'
const B = 'file:///Users/ryan/Downloads/big.iso/'
const NOW = Date.UTC(2026, 8, 29, 22, 15, 30)   // 2026-09-29T22:15:30Z
const ACTOR = 'ryan@runsascoded.com'

async function setup() {
  const { db } = await sqliteD1()
  await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', ?, 1)").bind(ACTOR).run()
  for (const p of [A, B]) {
    await db.prepare('INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, ?, 1)').bind(p, ACTOR).run()
  }
  const ex = laptopExecutor(() => NOW)
  const executors = { ...EXECUTORS, laptop: ex }
  return { db, ex, executors, env: { DB: db } }
}

const req = (o: Partial<DispatchReq>): DispatchReq => ({ planId: 1, mode: 'dry', actor: ACTOR, siteUrl: 'https://disk.rbw.sh', ...o })

const seen = (db: D1Database, ts: number) =>
  db.prepare("INSERT OR REPLACE INTO agents (name, seen_ts, host) VALUES (?, ?, 'm3')").bind(AGENT, ts).run()

const runs = async (db: D1Database) =>
  (await db.prepare('SELECT run_id, plan_id, manifest, scan, actor, mode, started_ts, finished_ts, log_dir, plan_digest FROM deletion_runs ORDER BY started_ts').all()).results

describe('the laptop executor', () => {
  it('accepts daily and sub-daily snapshot ids', () => {
    expect(['2026-09-29', '2026-09-29T1201', '20260929', '2026-09-29T12', '2026-09-29T120100'].map(d => DATE_RE.test(d))).toEqual([true, true, false, false, false])
  })

  it('keeps the exact sub-daily scan on the recorded run', async () => {
    const s = await setup()
    await seen(s.db, Math.floor(NOW / 1000))
    const digest = await planDigest([A, B])
    expect(await dispatchPlan(s.env, req({ date: '2026-09-29T1201' }), 'laptop', s.executors)).toEqual({
      ok: true, job_id: 'laptop-dry-20260929t221530z', plan_id: 1, mode: 'dry', date: '2026-09-29T1201', actor: ACTOR, digest,
      extra: { agent_seen: Math.floor(NOW / 1000) },
    })
    expect((await runs(s.db)).map(r => r.scan)).toEqual(['2026-09-29T1201'])
  })

  it('refuses while the drainer has never checked in, or not lately — nothing is recorded', async () => {
    const s = await setup()
    const never = await dispatchPlan(s.env, req({ date: '2026-09-29' }), 'laptop', s.executors)
    await seen(s.db, Math.floor(NOW / 1000) - STALE_S - 1)
    const stale = await dispatchPlan(s.env, req({ date: '2026-09-29' }), 'laptop', s.executors)
    expect([never, stale]).toEqual([
      { ok: false, status: 503, error: 'Cleanup agent disconnected. Request not submitted; the items remain staged. Open disky on the laptop, check its network connection, then check again.', extra: { code: 'cleanup_agent_offline', executor: 'laptop', ready: false, last_seen: null, checked_at: NOW / 1000, stale_after: STALE_S } },
      { ok: false, status: 503, error: 'Cleanup agent disconnected. Request not submitted; the items remain staged. Open disky on the laptop, check its network connection, then check again.', extra: { code: 'cleanup_agent_offline', executor: 'laptop', ready: false, last_seen: NOW / 1000 - STALE_S - 1, checked_at: NOW / 1000, stale_after: STALE_S } },
    ])
    expect(await runs(s.db)).toEqual([])
  })

  it('reports the same readiness boundary the dispatch gate uses', async () => {
    const s = await setup()
    const statuses = [await laptopStatus(s.db, NOW)]
    for (const age of [STALE_S, STALE_S + 1, 5]) {
      await seen(s.db, NOW / 1000 - age)
      statuses.push(await laptopStatus(s.db, NOW))
    }
    expect(statuses).toEqual([null, STALE_S, STALE_S + 1, 5].map(age => ({
      executor: 'laptop', ready: age != null && age <= STALE_S, last_seen: age == null ? null : NOW / 1000 - age, checked_at: NOW / 1000, stale_after: STALE_S,
    })))
  })

  it('freezes exactly the checked rows, preserving exact-object keys', async () => {
    const s = await setup()
    await seen(s.db, NOW / 1000)
    await s.db.prepare("UPDATE plan_items SET kind = 'object' WHERE prefix = ?").bind(B).run()
    const result = await dispatchPlan(s.env, req({ date: '2026-09-29', items: [`=${B}`] }), 'laptop', s.executors)
    expect(result).toEqual({
      ok: true, job_id: 'laptop-dry-20260929t221530z', plan_id: 1, mode: 'dry', date: '2026-09-29', actor: ACTOR,
      digest: await planDigest([`=${B}`]), extra: { agent_seen: NOW / 1000 },
    })
    expect((await s.db.prepare('SELECT selected_items_json FROM deletion_runs').all()).results).toEqual([{ selected_items_json: JSON.stringify([B]) }])
  })

  it('refuses an empty selection or a selected row removed since the page loaded', async () => {
    const s = await setup()
    await seen(s.db, NOW / 1000)
    expect([
      await dispatchPlan(s.env, req({ date: '2026-09-29', items: [] }), 'laptop', s.executors),
      await dispatchPlan(s.env, req({ date: '2026-09-29', items: ['file:///absent/'] }), 'laptop', s.executors),
    ]).toEqual([
      { ok: false, status: 400, error: 'plan has no items' },
      { ok: false, status: 400, error: 'not in the plan: file:///absent/' },
    ])
    expect(await runs(s.db)).toEqual([])
  })

  it('records a dry run for the drainer, then gates the real run on it', async () => {
    const s = await setup()
    const lastSeen = Math.floor(NOW / 1000) - 5
    await seen(s.db, lastSeen)
    const digest = await planDigest([A, B])

    const dry = await dispatchPlan(s.env, req({ date: '2026-09-29' }), 'laptop', s.executors)
    expect(dry).toEqual({
      ok: true, job_id: 'laptop-dry-20260929t221530z', plan_id: 1, mode: 'dry', date: '2026-09-29', actor: ACTOR, digest,
      extra: { agent_seen: lastSeen },
    })
    // a real run while the dry run is still in flight (the drainer hasn't finished it)
    expect(await dispatchPlan(s.env, req({ mode: 'real' }), 'laptop', s.executors)).toEqual(
      { ok: false, status: 409, error: 'not deleting: a dry run is in progress (laptop-dry-20260929t221530z)' },
    )
    // the drainer finishes it (what `disk_tree.drain` writes back)
    await s.db.prepare('UPDATE deletion_runs SET finished_ts = ?, deleted_bytes = 4096, deleted_objects = 3 WHERE run_id = ?')
      .bind(Math.floor(NOW / 1000) + 30, 'laptop-dry-20260929t221530z').run()
    const real = await dispatchPlan(s.env, req({ mode: 'real' }), 'laptop', s.executors)
    expect(real).toEqual({
      ok: true, job_id: 'laptop-real-20260929t221530z', plan_id: 1, mode: 'real', date: '2026-09-29', actor: ACTOR, digest,
      extra: { agent_seen: lastSeen },
    })
    expect(await runs(s.db)).toEqual([
      { run_id: 'laptop-dry-20260929t221530z', plan_id: 1, manifest: 'laptop', scan: '2026-09-29', actor: ACTOR, mode: 'dry', started_ts: Math.floor(NOW / 1000), finished_ts: Math.floor(NOW / 1000) + 30, log_dir: 'laptop', plan_digest: digest },
      { run_id: 'laptop-real-20260929t221530z', plan_id: 1, manifest: 'laptop', scan: '2026-09-29', actor: ACTOR, mode: 'real', started_ts: Math.floor(NOW / 1000), finished_ts: null, log_dir: 'laptop', plan_digest: digest },
    ])
  })

  it('refuses an unknown or empty plan', async () => {
    const s = await setup()
    await seen(s.db, Math.floor(NOW / 1000))
    await s.db.prepare('DELETE FROM plan_items WHERE plan_id = 1').run()
    expect([
      await dispatchPlan(s.env, req({ planId: 2, date: '2026-09-29' }), 'laptop', s.executors),
      await dispatchPlan(s.env, req({ date: '2026-09-29' }), 'laptop', s.executors),
    ]).toEqual([
      { ok: false, status: 404, error: 'no such plan' },
      { ok: false, status: 400, error: 'plan has no items' },
    ])
  })

  it('has nothing to reflect: the drainer writes results itself', async () => {
    const s = await setup()
    expect(await s.ex.refresh(s.env, s.db)).toEqual([])
  })
})
