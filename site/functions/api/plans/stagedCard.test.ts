// `POST /api/plans/staged/card`: a read route must not write to Slack, so this one is the job's named grant's alone.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'
import { onRequest } from './[[path]]'

const { refresh } = vi.hoisted(() => ({ refresh: vi.fn(async () => true) }))
vi.mock('../../_lib/stagedSlack.js', async original => ({ ...await original<typeof import('../../_lib/stagedSlack.js')>(), refreshStagedCard: refresh }))

let env: Record<string, unknown>
const mint = (name: string, scopes: string[]) => gateFor(env as never)!.mint({ name, scopes, createdBy: 'test', expiresAt: null, maxRedeems: null })
let job: Awaited<ReturnType<typeof mint>>

beforeEach(async () => {
  env = { DB: (await sqliteD1()).db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  job = await mint('scan job', ['cw:read'])
  refresh.mockClear()
})

const post = async (token: string | null, origin = 'https://site.example.test') => {
  const r = await onRequest({
    request: new Request(`${origin}/api/plans/staged/card`, { method: 'POST', headers: token ? { authorization: `Bearer ${token}` } : {} }),
    env: env as never,
  } as never)
  return { status: r.status, body: await r.json(), refreshes: refresh.mock.calls.length }
}

describe('the staged-card ping', () => {
  it("re-renders on the configured grant's call, from the origin it called", async () => {
    env.STAGED_CARD_GRANT = job.grant.id
    expect(await post(job.token)).toEqual({ status: 200, body: { refreshed: true }, refreshes: 1 })
    expect((refresh.mock.calls[0] as unknown[])[2]).toBe('https://site.example.test')
  })
  it('is not there without a configured grant, whoever asks', async () => {
    const admin = await mint('ops', ['cw', 'admin'])
    const gone = { status: 404, body: { error: 'no staged-card grant on this deployment' }, refreshes: 0 }
    expect([await post(job.token), await post(admin.token), await post(null)]).toEqual([gone, gone, gone])
  })
  it("refuses a signed-in admin: a session isn't the grant", async () => {
    env.STAGED_CARD_GRANT = job.grant.id
    // (a localhost request is the dev identity: a session with every scope, admin included)
    expect(await post(null, 'http://localhost')).toEqual({ status: 403, body: { error: 'forbidden' }, refreshes: 0 })
  })
  it("refuses the named grant itself once it isn't this deployment's viewer", async () => {
    const other = await mint('scan job', ['gcs:read'])
    env.STAGED_CARD_GRANT = other.grant.id
    expect(await post(other.token)).toEqual({ status: 403, body: { error: 'forbidden' }, refreshes: 0 })
  })
  it('refuses any other viewer, even an admin', async () => {
    env.STAGED_CARD_GRANT = job.grant.id
    const reader = await mint('share link', ['cw:read'])
    const admin = await mint('ops', ['cw', 'admin'])
    expect([await post(reader.token), await post(admin.token)]).toEqual([
      { status: 403, body: { error: 'forbidden' }, refreshes: 0 },
      { status: 403, body: { error: 'forbidden' }, refreshes: 0 },
    ])
  })
  it('refuses no token at all', async () => {
    env.STAGED_CARD_GRANT = job.grant.id
    expect(await post(null)).toEqual({ status: 401, body: { error: 'unauthenticated' }, refreshes: 0 })
  })
})
