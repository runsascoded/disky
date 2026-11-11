// `POST /api/scan-runs/dispatch` ("Run a scan", `_lib/scanDispatch.ts`): the Cloud Run call it makes, what refuses
// it (a running scan, a pending dispatch), the claim two clicks race for, and its log; the list's `dispatch`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type Env, gateFor } from '../_lib/auth'
import { claim, PENDING_S, settle } from '../_lib/scanDispatch'
import { type Sqlite, sqliteD1, withAdminEmails, withScanDispatch } from '../_lib/testD1'
import { onRequestPost as dispatch } from './scan-runs/dispatch'
import { onRequestGet as list } from './scan-runs/[[path]]'

vi.mock('../_lib/gcp', async orig => ({ ...(await orig<typeof import('../_lib/gcp')>()), gcpToken: async () => 'tok' }))

const SITE = 'https://gcs.example.test'
const NOW = 1_800_000_000
const ADMIN = 'lead@example.org'
const JOB = 'projects/demo-project/locations/us-central1/jobs/demo-snapshot-submit'
const EXEC = `${JOB}/executions/demo-snapshot-submit-x7k2p`
const OP = { name: 'projects/demo-project/locations/us-central1/operations/0f1e', metadata: { '@type': 'type.googleapis.com/google.cloud.run.v2.Execution', name: EXEC } }

beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW * 1000) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

interface Call { url: string; method: string; auth: string | null; body: unknown }

/** A deployment with `ADMIN` on its admin list and the dispatch configured; Cloud Run answers `answer`. `table`: its D1
 *  has `scan_dispatch` (the recipe applied). */
async function world(answer: () => Response = () => Response.json(OP), extra: Record<string, unknown> = {}, table = true) {
  const { db, raw } = await sqliteD1()
  withAdminEmails(raw)
  if (table) withScanDispatch(raw)
  else raw.exec('DROP TABLE IF EXISTS scan_dispatch')
  raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(ADMIN)
  const env = { DB: db, SESSION_SECRET: 'secret', BASE_SCOPE: 'gcs', ADMIN_LIST: '1', GCP_SA_KEY: '{}', SCAN_SUBMIT_JOB: JOB, ...extra } as Env
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, method: init.method ?? 'GET', auth: new Headers(init.headers).get('authorization'), body: JSON.parse(String(init.body)) })
    return answer()
  })
  const cookie = (await gateFor(env)!.signIn(ADMIN, new Request(SITE)))!.cookie.split(';')[0]
  const post = async (): Promise<[number, unknown]> => {
    const r = await dispatch({ request: new Request(`${SITE}/api/scan-runs/dispatch`, { method: 'POST', headers: { cookie } }), env })
    return [r.status, await r.json()]
  }
  const get = async (): Promise<unknown> =>
    (await (await list({ request: new Request(`${SITE}/api/scan-runs`, { headers: { cookie } }), env, params: {} } as never)).json() as { dispatch: unknown }).dispatch
  return { raw, calls, post, get }
}

/** The dispatches (`scan_dispatch`), oldest first, each id (a fresh uuid) checked and shown as `ID`. */
const log = (raw: Sqlite): Record<string, unknown>[] => raw.prepare('SELECT * FROM scan_dispatch ORDER BY ts, rowid').all()
  .map(r => ({ ...r, id: /^[0-9a-f-]{36}$/.test(String(r.id)) ? 'ID' : r.id }))
const run = (raw: Sqlite, id: string, kind: string, status: string, started: number, updated: number, store = 'primary', parent: string | null = null) =>
  raw.prepare("INSERT INTO scan_runs (run_id, scan, kind, parent, status, started_ts, source, updated_ts, store) VALUES (?, '2026-11-11T0700', ?, ?, ?, ?, 'job', ?, ?)")
    .run(id, kind, parent, status, started, updated, store)

describe('POST /api/scan-runs/dispatch', () => {
  it('runs the submitter job with DT_TRIGGER=manual, logs the dispatch and returns the execution', async () => {
    const w = await world()
    expect(await w.post()).toEqual([200, { execution: EXEC, operation: OP.name }])
    expect(w.calls).toEqual([{
      url: `https://run.googleapis.com/v2/${JOB}:run`, method: 'POST', auth: 'Bearer tok',
      body: { overrides: { containerOverrides: [{ env: [{ name: 'DT_TRIGGER', value: 'manual' }] }] } },
    }])
    expect(log(w.raw)).toEqual([
      { id: 'ID', ts: NOW, who: ADMIN, job: JOB, state: 'dispatched', execution: EXEC, operation: OP.name, error: null, updated_ts: NOW },
    ])
    // nothing in the admin table log: `admin_edits.action` is `insert | update | delete` on most deployments' schemas
    expect(w.raw.prepare('SELECT count(*) AS n FROM admin_edits').all()).toEqual([{ n: 0 }])
  })

  it('refuses while a scan of the primary store runs; a lost one, a maintenance job or another store’s scan doesn’t count', async () => {
    const w = await world()
    run(w.raw, 'lost', 'scan', 'running', NOW - 7200, NOW - 1801)
    run(w.raw, 'compact-1', 'compact', 'running', NOW - 60, NOW - 30)
    run(w.raw, 'ingest-1', 'access-ingest', 'running', NOW - 60, NOW - 30)
    run(w.raw, 'meta-1', 'scan', 'running', NOW - 60, NOW - 30, 'meta')
    run(w.raw, 'live', 'scan', 'running', NOW - 600, NOW - 30)
    run(w.raw, 'live-l', 'listing', 'running', NOW - 500, NOW - 30, 'primary', 'live')
    expect(await w.post()).toEqual([409, {
      error: 'a scan is already running (2026-11-11T0700, run live, started 10 min ago); start another once it finishes',
      running: { run_id: 'live', scan: '2026-11-11T0700', started_ts: NOW - 600 },
    }])
    w.raw.exec("UPDATE scan_runs SET status = 'succeeded' WHERE run_id = 'live'")
    expect((await w.post())[0]).toBe(200)
    expect(w.calls.length).toBe(1)
  })

  it('a second dispatch waits for the first’s run: refused until a scan run starts, or the pending window passes', async () => {
    const w = await world()
    expect((await w.post())[0]).toBe(200)
    vi.setSystemTime((NOW + 120) * 1000)
    const pending = 'a scan was started 2 min ago by lead@example.org and hasn\'t recorded its run yet; another can start once it does, '
      + 'or 58 min from now if it never does (its submitter alerts when a submit fails)'
    expect(await w.post()).toEqual([409, { error: pending, pending: { id: expect.any(String), ts: NOW, who: ADMIN, execution: EXEC } }])
    // its run recorded and finished: the next may go
    run(w.raw, 'first', 'scan', 'succeeded', NOW + 60, NOW + 100)
    expect((await w.post())[0]).toBe(200)
    // this one's run never records: past the window it blocks nothing
    vi.setSystemTime((NOW + 120 + PENDING_S + 1) * 1000)
    expect((await w.post())[0]).toBe(200)
    expect(w.calls.length).toBe(3)
  })

  it('a double click starts one scan', async () => {
    const w = await world()
    const [a, b] = await Promise.all([w.post(), w.post()])
    expect([a[0], b[0]].sort()).toEqual([200, 409])
    expect(w.calls.length).toBe(1)
  })

  it('the claim is the lock: of two past the checks, one wins; a failed one releases it', async () => {
    const { db, raw } = await sqliteD1()
    withScanDispatch(raw)
    const tries = [await claim(db, 'a', ADMIN, NOW, JOB), await claim(db, 'b', ADMIN, NOW, JOB)]
    await settle(db, 'a', NOW + 5, { state: 'failed', error: 'x' })
    expect([...tries, await claim(db, 'c', ADMIN, NOW + 6, JOB)]).toEqual([true, false, true])
    expect(log(raw).map(r => [r.id, r.state, r.error, r.updated_ts])).toEqual([['a', 'failed', 'x', NOW + 5], ['c', 'claimed', null, NOW + 6]])
  })

  it('a refusal from Cloud Run is a 500 with its detail, and releases the claim', async () => {
    let status = 403
    const w = await world(() => (status === 200 ? Response.json(OP) : new Response('{"error":{"code":403,"message":"Permission denied"}}', { status })))
    expect(await w.post()).toEqual([500, { error: 'scan dispatch failed (Cloud Run 403)', detail: '{"error":{"code":403,"message":"Permission denied"}}' }])
    expect(log(w.raw)).toEqual([{
      id: 'ID', ts: NOW, who: ADMIN, job: JOB, state: 'failed', execution: null, operation: null,
      error: 'Cloud Run 403: {"error":{"code":403,"message":"Permission denied"}}', updated_ts: NOW,
    }])
    status = 200
    expect(await w.post()).toEqual([200, { execution: EXEC, operation: OP.name }])
    expect(log(w.raw).map(r => r.state)).toEqual(['failed', 'dispatched'])
  })

  it('unconfigured: 503 naming what’s missing, before any call', async () => {
    const answers = await Promise.all([
      world(undefined, { SCAN_SUBMIT_JOB: undefined }), world(undefined, { SCAN_SUBMIT_JOB: 'demo-snapshot-submit' }), world(undefined, { GCP_SA_KEY: undefined }),
    ].map(async p => { const w = await p; return [...(await w.post()), w.calls.length] }))
    expect(answers).toEqual([
      [503, { error: 'scan dispatch not configured (SCAN_SUBMIT_JOB unset or not projects/<p>/locations/<r>/jobs/<job>)' }, 0],
      [503, { error: 'scan dispatch not configured (SCAN_SUBMIT_JOB unset or not projects/<p>/locations/<r>/jobs/<job>)' }, 0],
      [503, { error: 'scan dispatch not configured (GCP_SA_KEY unset)' }, 0],
    ])
  })

  it('a D1 without `scan_dispatch` (the recipe unapplied): 503 naming it, before any call', async () => {
    const w = await world(undefined, {}, false)
    expect([...(await w.post()), w.calls.length]).toEqual([503, { error: 'scan dispatch is not set up on this deployment (no scan_dispatch table)' }, 0])
  })
})

describe('GET /api/scan-runs: dispatch', () => {
  it('says what would refuse a dispatch now; null where there is none to make', async () => {
    const w = await world()
    expect(await w.get()).toEqual({ running: null, pending_since: null })
    await w.post()
    expect(await w.get()).toEqual({ running: null, pending_since: NOW })
    run(w.raw, 'live', 'scan', 'running', NOW + 1, NOW + 1)
    expect(await w.get()).toEqual({ running: 'live', pending_since: null })
    expect(await (await world(undefined, { SCAN_SUBMIT_JOB: undefined })).get()).toBeNull()
    // no `scan_dispatch` table: no button, as with no `scan_runs`
    expect(await (await world(undefined, {}, false)).get()).toBeNull()
  })
})
