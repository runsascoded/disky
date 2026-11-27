// A D1 without the `digest-thread-variant` recipe (a deployment that hasn't applied it yet): the digest routes read its
// columns as null and its table as no row, and a write that needs them is a 503 naming the recipe.
import { beforeAll, describe, expect, it } from 'vitest'
import { onRequestGet as getReapply } from '../api/digest-reapply'
import { onRequestGet as getSettings, onRequestPut as putSettings } from '../api/digest-settings'
import { onRequestGet as getThreads, onRequestPut as putThreads } from '../api/digest-threads'
import { gateFor } from './auth'
import { hasThreadVariant, RECIPE_MISSING } from './digestRecipe'
import { sqliteD1 } from './testD1'

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
    expect(await hasThreadVariant(env.DB as never)).toBe(false)
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
