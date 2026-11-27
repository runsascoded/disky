// A D1 without the `digest-thread-variant` recipe (a deployment that hasn't applied it yet): the digest routes read its
// columns as null and its table as no row, and a write that needs them is a 503 naming the recipe.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { onRequestGet as getReapply } from '../api/digest-reapply'
import { onRequestGet as getSettings, onRequestPut as putSettings } from '../api/digest-settings'
import { onRequestGet as getThreads, onRequestPut as putThreads } from '../api/digest-threads'
import { gateFor } from './auth'
import { hasThreadSwitch, hasThreadVariant, missingRecipe, RECIPE_MISSING } from './digestRecipe'
import { sqliteD1 } from './testD1'

vi.mock('./gcp.js', () => ({ gcpToken: async () => 'gcp-token' }))
afterEach(() => vi.unstubAllGlobals())

let env: Record<string, unknown>
let admin: string
let viewer: string

beforeAll(async () => {
  const d1 = await sqliteD1()
  // undo the recipe: the D1 as it was before
  d1.raw.exec('DROP TABLE digest_threads; ALTER TABLE digest_settings DROP COLUMN variant; ALTER TABLE digest_reapply DROP COLUMN variant')
  d1.raw.prepare('INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, params, quota_marks, actor, ts, out) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)')
    .run('quota-digest-20261127-120000z', '2026-11', 'C0C9J6CE8BD', 'op', 1, 'ops@example.test', 100, 'gs://data/x.txt')
  env = { DB: d1.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', DATA_BUCKET: 'data', GCP_SA_KEY: '{}' }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['cw', 'admin'])
  viewer = await tok('ann@example.test', ['cw'])
})

const call = async (f: (ctx: never) => Promise<Response>, method: string, path: string, token: string, body?: unknown) => {
  const r = await f({ request: new Request(`https://site.example.test${path}`, {
    method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  }), env } as never)
  return [r.status, await r.json()] as const
}

describe('before the digest-thread-variant recipe', () => {
  it('is detected', async () => {
    expect([await hasThreadVariant(env.DB as never), await hasThreadSwitch(env.DB as never)]).toEqual([false, false])
  })

  it('reads a thread as no row, and refuses to set one', async () => {
    expect(await call(getThreads, 'GET', '/api/digest-threads?channel=C0C9J6CE8BD&month=2026-11', viewer)).toEqual([200, { thread: null }])
    expect(await call(putThreads, 'PUT', '/api/digest-threads', admin, { channel: 'C0C9J6CE8BD', month: '2026-11', variant: 'weekly', plan_job: 'quota-digest-20261127-120000z' }))
      .toEqual([503, { error: RECIPE_MISSING }])
  })

  it('reads the settings’ variant as null, saves settings without one, and refuses one', async () => {
    const s = { template: 'quota', params: '', quota_marks: 'runway' }
    expect(await call(putSettings, 'PUT', '/api/digest-settings', admin, { ...s, variant: 'weekly' })).toEqual([503, { error: RECIPE_MISSING }])
    const [st, b] = await call(putSettings, 'PUT', '/api/digest-settings', admin, s)
    expect([st, { ...(b as { settings: object }).settings, updated_at: '<ts>' }]).toEqual([200, { ...s, variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
    const [gt, g] = await call(getSettings, 'GET', '/api/digest-settings?template=quota', viewer)
    expect([gt, { ...(g as { settings: object }).settings, updated_at: '<ts>' }]).toEqual([200, { ...s, variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
  })

  it('lists re-apply runs with a null variant', async () => {
    const [st, b] = await call(getReapply, 'GET', '/api/digest-reapply', admin)
    expect([st, (b as { runs: { job_id: string; variant: unknown }[] }).runs.map(r => [r.job_id, r.variant])]).toEqual([200, [['quota-digest-20261127-120000z', null]]])
  })
})

describe('with digest-thread-variant but before digest-thread-switch', () => {
  const JOB = 'quota-digest-20261127-130000z'
  let senv: Record<string, unknown>
  let sadmin: string
  beforeAll(async () => {
    const d1 = await sqliteD1()
    d1.raw.exec('ALTER TABLE digest_threads DROP COLUMN edits; ALTER TABLE digest_threads DROP COLUMN reorder')
    d1.raw.prepare('INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, params, quota_marks, variant, actor, ts, out) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)')
      .run(JOB, '2026-11', 'C0C9J6CE8BD', 'thread', 1, 'weekly', 'ops@example.test', 9_999_999_999, 'gs://data/x.txt')
    senv = { DB: d1.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', DATA_BUCKET: 'data', GCP_SA_KEY: '{}' }
    sadmin = (await gateFor(senv as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  })
  const plan = { digest_switch: 1, month: '2026-11', channel: 'C0C9J6CE8BD', from: 'sender', to: 'weekly', at: 't', post: [], delete: [{ key: 'd', ts: '1.000001' }] }
  const put = async (p: object) => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify(p), { status: 200 }))
    const r = await putThreads({ request: new Request('https://site.example.test/api/digest-threads', {
      method: 'PUT', headers: { authorization: `Bearer ${sadmin}`, 'content-type': 'application/json' },
      body: JSON.stringify({ channel: 'C0C9J6CE8BD', month: '2026-11', variant: 'weekly', plan_job: JOB, allow_reorder: true }),
    }), env: senv } as never)
    return [r.status, await r.json()] as const
  }

  it('sets a switch with no edits or reorders, reading both as none; one with either is a 503', async () => {
    expect(await hasThreadSwitch(senv.DB as never)).toBe(false)
    expect(await put({ ...plan, edit: [{ key: 'e', ts: '1.000002', to: 'e', scan: 's', was: 'a', headline: 'b' }] })).toEqual([503, { error: missingRecipe('digest-thread-switch') }])
    const [s, b] = await put(plan)
    expect([s, { ...(b as { thread: object }).thread, updated_at: '<ts>' }]).toEqual([200, {
      channel: 'C0C9J6CE8BD', month: '2026-11', variant: 'weekly', plan_job: JOB, deletes: ['1.000001'], edits: [], reorder: [], updated_by: 'ops@example.test', updated_at: '<ts>',
    }])
  })
})
