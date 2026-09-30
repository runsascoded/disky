/** The staged-delete edge layer (spec `specs/staged-delete.md`, CP2): the D1
 *  data layer (`cfn/staged.ts`) directly, and the routes (`cfn/stagedRoutes.ts`)
 *  through the *real* gate over an in-memory D1 — the same scope checks the
 *  middleware runs, so `view` stages and only `admin` dispatches. */
import { beforeEach, describe, expect, it } from 'vitest'
import { migratedD1 } from './fakeD1'
import type { Env } from '../env'
import { VIEW_SCOPE, gateFor } from '../auth'
import * as S from '../staged'
import { getStaged, postDispatch, postStage, postUnstage } from '../stagedRoutes'

const A = 'r2://bucket/a'
const B = 'r2://bucket/b'
const C = 'r2://bucket/c'
const ADMIN = 'ryan@example.test'

/** Minimal in-memory R2 for the CFN dispatch path (CP7). */
class FakeR2 {
  objects = new Map<string, number>()
  deleted: string[] = []
  seed(entries: Record<string, number>) {
    for (const [k, v] of Object.entries(entries)) this.objects.set(k, v)
    return this
  }
  async head(key: string) {
    return this.objects.has(key) ? { key, size: this.objects.get(key)! } : null
  }
  async list({ prefix = '' }: { prefix?: string }) {
    const objects = [...this.objects.entries()].filter(([k]) => k.startsWith(prefix)).map(([key, size]) => ({ key, size }))
    return { objects, truncated: false, cursor: undefined, delimitedPrefixes: [] }
  }
  async delete(keys: string | string[]) {
    for (const k of Array.isArray(keys) ? keys : [keys]) {
      this.objects.delete(k)
      this.deleted.push(k)
    }
  }
}

let db: D1Database
beforeEach(() => {
  db = migratedD1()
})

// ---- data layer -----------------------------------------------------------

describe('cfn/staged (D1 data layer)', () => {
  it('stages idempotently into one shared open plan', async () => {
    const first = await S.stage(db, [A, B], 'ryan')
    const second = await S.stage(db, [B, C], 'ryan')
    expect(first.plan.id).toBe(second.plan.id)
    expect([first.added, second.added]).toEqual([[A, B], [C]])
    expect((await S.planItems(db, first.plan.id)).map(i => i.uri)).toEqual([A, B, C])
  })

  it('keeps a plan free of nesting: descendants skipped, ancestors absorb', async () => {
    const child = `${A}/x/y.zip`
    const sibling = 'r2://bucket/ab'
    expect((await S.stage(db, [child, sibling], 'ryan')).added).toEqual([child, sibling])
    const { plan, added } = await S.stage(db, [A], 'ryan')
    expect(added).toEqual([A])
    expect((await S.planItems(db, plan.id)).map(i => i.uri).sort()).toEqual([A, sibling])
    expect((await S.stage(db, [`${A}/x`], 'ryan')).added).toEqual([])
    expect(S.uncovered([A, `${A}/z`, sibling])).toEqual([A, sibling])
  })

  it('canonicalizes a trailing slash but keeps the scheme', async () => {
    const { added } = await S.stage(db, ['r2://bucket/d/'], 'ryan')
    expect(added).toEqual(['r2://bucket/d'])
  })

  it('unstages from the open plan and counts removals', async () => {
    const { plan } = await S.stage(db, [A, B], 'ryan')
    expect(await S.unstage(db, [A])).toBe(1)
    expect(await S.unstage(db, [A])).toBe(0)
    expect((await S.planItems(db, plan.id)).map(i => i.uri)).toEqual([B])
  })

  it('resolves a plan by id, name, default, and miss', async () => {
    const { plan } = await S.stage(db, [A], 'ryan')
    expect((await S.planByRef(db, null))?.id).toBe(plan.id)
    expect((await S.planByRef(db, String(plan.id)))?.id).toBe(plan.id)
    expect((await S.planByRef(db, 'Staged'))?.id).toBe(plan.id)
    expect(await S.planByRef(db, 'nope')).toBeNull()
  })

  it('enqueues a dispatch: a pending real run, plan closed, no bands', async () => {
    const { plan } = await S.stage(db, [A, B], 'ryan')
    const run = await S.enqueueDispatch(db, plan, ADMIN)
    expect(run.run_id.startsWith(`${plan.id}-`)).toBe(true)
    expect([run.mode, run.finished_ts, run.deleted_bytes, run.actor]).toEqual(['real', null, 0, ADMIN])
    expect((await S.planByRef(db, String(plan.id)))?.state).toBe('closed')
    expect((await S.listOpenPlans(db)).length).toBe(0)
    const bands = await db.prepare('SELECT * FROM deletion_bands WHERE run_id = ?1').bind(run.run_id).all()
    expect(bands.results).toEqual([]) // the server-side executor lands bands, not the edge
    const runs = await S.listRuns(db)
    expect(runs.map(r => [r.run_id, r.mode, r.finished_ts])).toEqual([[run.run_id, 'real', null]])
  })
})

// ---- routes + auth --------------------------------------------------------

const req = () => new Request('https://dt.test/x')

function envOf(open = false): Env {
  const base = { SCANS: undefined, DB: db, SESSION_SECRET: 'test-secret', ALLOWED_EMAILS: ADMIN }
  return (open ? { ...base, PUBLIC_OPEN: '1' } : base) as unknown as Env
}

async function adminCookie(env: Env): Promise<string> {
  const signed = await gateFor(env, req()).gate.signIn(ADMIN, req())
  return signed!.cookie.split(';')[0]
}

async function viewerCookie(env: Env): Promise<string> {
  const gate = gateFor(env, req()).gate
  const { token } = await gate.mint({ name: 'viewer', scopes: [VIEW_SCOPE], createdBy: ADMIN })
  const r = await gate.redeem(token, req())
  if (!r.ok) throw new Error('redeem failed')
  return r.cookie.split(';')[0]
}

function request(cookie: string | null, body?: unknown): Request {
  const headers = new Headers()
  if (cookie) headers.set('cookie', cookie)
  const init: RequestInit = { method: body === undefined ? 'GET' : 'POST', headers }
  if (body !== undefined) {
    headers.set('content-type', 'application/json')
    init.body = JSON.stringify(body)
  }
  return new Request('https://dt.test/api/staged', init)
}

async function read(r: Response): Promise<{ status: number; body: unknown }> {
  const text = await r.text()
  let body: unknown = text
  try {
    body = JSON.parse(text)
  } catch {
    /* not JSON */
  }
  return { status: r.status, body }
}

describe('staged routes (real gate)', () => {
  it('refuses an anonymous stage/dispatch', async () => {
    const env = envOf()
    expect(await read(await postStage(env, request(null, { uris: [A] })))).toEqual({ status: 401, body: { error: 'unauthenticated' } })
    expect(await read(await postDispatch(env, request(null, {})))).toEqual({ status: 401, body: { error: 'unauthenticated' } })
  })

  it('lets a `view` session stage and read back the staged set', async () => {
    const env = envOf()
    const cookie = await viewerCookie(env)
    expect(await read(await postStage(env, request(cookie, { uris: [A, B] })))).toEqual({ status: 200, body: { plan_id: 1, added: [A, B] } })

    const listed = await read(await getStaged(env))
    expect(listed.status).toBe(200)
    const { plans, runs } = listed.body as { plans: { id: number; state: string; items: { uri: string; bytes: null; objects: null; kind: null }[]; created_by: string }[]; runs: unknown[] }
    expect(plans.map(p => [p.id, p.state, p.items])).toEqual([[1, 'open', [{ uri: A, bytes: null, objects: null, kind: null }, { uri: B, bytes: null, objects: null, kind: null }]]])
    expect(plans[0].created_by).toBe('viewer')
    expect(runs).toEqual([])
  })

  it('rejects a stage with no `uris`', async () => {
    const env = envOf()
    const cookie = await viewerCookie(env)
    expect(await read(await postStage(env, request(cookie, { note: 'x' })))).toEqual({ status: 400, body: { error: '`uris` must be a non-empty array of strings' } })
  })

  it('lets a `view` session unstage', async () => {
    const env = envOf()
    const cookie = await viewerCookie(env)
    await postStage(env, request(cookie, { uris: [A, B] }))
    expect(await read(await postUnstage(env, request(cookie, { uris: [A] })))).toEqual({ status: 200, body: { removed: 1 } })
    const listed = (await read(await getStaged(env))).body as { plans: { items: { uri: string }[] }[] }
    expect(listed.plans.map(p => p.items.map(i => i.uri))).toEqual([[B]])
  })

  it('forbids `view` from dispatching, admits `admin`', async () => {
    const env = envOf()
    const viewer = await viewerCookie(env)
    await postStage(env, request(viewer, { uris: [A, B] }))

    expect(await read(await postDispatch(env, request(viewer, {})))).toEqual({ status: 403, body: { error: 'forbidden' } })

    const admin = await adminCookie(env)
    const dispatched = await read(await postDispatch(env, request(admin, {})))
    expect(dispatched.status).toBe(200)
    const d = dispatched.body as { run_id: string; plan_id: number; mode: string; items: number; state: string }
    expect([d.plan_id, d.mode, d.items, d.state]).toEqual([1, 'real', 2, 'enqueued'])
    expect(d.run_id.startsWith('1-')).toBe(true)

    // plan closed, run recorded (pending), staged set now empty
    const after = await read(await getStaged(env))
    const ab = after.body as { plans: unknown[]; runs: { run_id: string; mode: string; finished_ts: number | null; actor: string }[] }
    expect(ab.plans).toEqual([])
    expect(ab.runs.map(r => [r.run_id, r.mode, r.finished_ts, r.actor])).toEqual([[d.run_id, 'real', null, ADMIN]])
  })

  it('dispatch deletes small same-account R2 inline (CFN) instead of enqueueing', async () => {
    const env = envOf()
    const bucket = new FakeR2().seed({ 'logs/a': 10, 'logs/b': 20 })
    ;(env as unknown as Record<string, unknown>).R2_ctbk = bucket
    const admin = await adminCookie(env)
    await postStage(env, request(admin, { uris: ['r2://ctbk/logs'] }))

    const res = await read(await postDispatch(env, request(admin, {})))
    expect(res.status).toBe(200)
    const d = res.body as { state: string; deleted_objects: number; deleted_bytes: number; run_id: string }
    expect([d.state, d.deleted_objects, d.deleted_bytes]).toEqual(['done', 2, 30])
    expect(bucket.deleted.sort()).toEqual(['logs/a', 'logs/b'])

    // the run is recorded *finished* (not pending), plan closed
    const after = (await read(await getStaged(env))).body as { plans: unknown[]; runs: { run_id: string; finished_ts: number | null }[] }
    expect(after.plans).toEqual([])
    expect(after.runs.map(r => [r.run_id, r.finished_ts !== null])).toEqual([[d.run_id, true]])
  })

  it('dispatch enqueues (drainer) when the bucket is unbound or too big', async () => {
    const env = envOf()
    const bucket = new FakeR2().seed({ 'big/1': 1, 'big/2': 1, 'big/3': 1 })
    ;(env as unknown as Record<string, unknown>).R2_ctbk = bucket
    ;(env as unknown as Record<string, unknown>).DELETE_THRESHOLD = '2'
    const admin = await adminCookie(env)
    await postStage(env, request(admin, { uris: ['r2://ctbk/big'] }))

    const res = await read(await postDispatch(env, request(admin, {})))
    expect((res.body as { state: string }).state).toBe('enqueued') // over threshold → drainer
    expect(bucket.deleted).toEqual([]) // nothing deleted at the edge
  })

  it('refuses a dispatch of an empty / missing plan', async () => {
    const env = envOf()
    const admin = await adminCookie(env)
    expect(await read(await postDispatch(env, request(admin, {})))).toEqual({ status: 404, body: { error: 'no plan (open Staged)' } })
    expect(await read(await postDispatch(env, request(admin, { plan: '999' })))).toEqual({ status: 404, body: { error: 'no plan 999' } })
  })

  it('is unavailable on the open (unauthenticated) demo', async () => {
    const env = envOf(true)
    const msg = 'staged delete is unavailable on the open (unauthenticated) demo'
    expect(await read(await getStaged(env))).toEqual({ status: 501, body: { error: msg } })
    expect(await read(await postStage(env, request(null, { uris: [A] })))).toEqual({ status: 501, body: { error: msg } })
  })
})
