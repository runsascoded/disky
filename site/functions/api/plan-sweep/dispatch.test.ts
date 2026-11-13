// `POST /api/plan-sweep/dispatch`'s announcement, once the job has launched.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'
import { onRequestPost } from './dispatch'

const { announced, pathStore } = vi.hoisted(() => ({ announced: vi.fn(async () => {}), pathStore: vi.fn() }))
const LAUNCHED = { ok: true, job_id: 'j', plan_id: 1, mode: 'real', date: '2026-11-04', actor: 'ops', digest: 'd', extra: {} }
vi.mock('../../_lib/executor.js', async original => ({
  ...await original<typeof import('../../_lib/executor.js')>(),
  dispatchPlan: async () => LAUNCHED,
  notifyDispatched: announced,
}))
vi.mock('../../_lib/index.js', async original => ({ ...await original<typeof import('../../_lib/index.js')>(), withPathStore: pathStore }))

let env: Record<string, unknown>
let token: string

beforeEach(async () => {
  const { db } = await sqliteD1()
  env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', EXECUTOR: 'plan-sweep' }
  token = (await gateFor(env as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  announced.mockClear()
})
afterEach(() => vi.clearAllMocks())

const post = () => onRequestPost({
  request: new Request('https://site.example.test/api/plan-sweep/dispatch', {
    method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ plan_id: 1, mode: 'real', date: '2026-11-04' }),
  }),
  env: env as never,
})

describe('the announcement', () => {
  it('is sized through `withPathStore`', async () => {
    const stored = { ...env, PATH_STORE: 'intervals' }
    pathStore.mockResolvedValueOnce({ env: stored })
    const r = await post()
    expect(r.status).toBe(200)
    expect(announced.mock.calls.map(c => (c as unknown[])[0])).toEqual([stored])
  })
  it('falls back to the raw env when `withPathStore` rejects after the job launched', async () => {
    pathStore.mockRejectedValueOnce(new Error('pointer read failed'))
    const r = await post()
    expect(r.status).toBe(200)
    expect(announced.mock.calls.map(c => (c as unknown[])[0])).toEqual([env])
  })
})
