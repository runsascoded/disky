import { expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { onRequestPost } from './prefixes'

const { prefixesAt } = vi.hoisted(() => ({ prefixesAt: vi.fn() }))
vi.mock('../_lib/index.js', () => ({
  ivRefusal: () => null,
  storeReady: () => true,
  withPathStore: async (ctx: { request: Request; env: Env }) => ({ ...ctx, env: { ...ctx.env, INTERVAL_STORE_GEN: 'current-gen' } }),
}))
vi.mock('../_lib/prefixes.js', () => ({ MAX_PREFIXES: 1000, prefixesAt }))
vi.mock('../_lib/scanArg.js', () => ({ scanArg: async () => '2026-10-21T1801' }))

it('sizes staged prefixes from the current interval generation after per-scan files are retired', async () => {
  const prefix = 's3://bucket/users/kept/'
  prefixesAt.mockImplementation(async (env: Env) => {
    if (env.INTERVAL_STORE_GEN !== 'current-gen') throw new Error("index variant 'path' not synced for 2026-10-21T1801")
    return { stats: { [prefix]: { bytes: 90, objects: 9 } }, groups: 1 }
  })
  const response = await onRequestPost({
    request: new Request('http://localhost/api/prefixes', {
      method: 'POST', body: JSON.stringify({ date: '2026-10-21T1801', prefixes: [prefix] }),
    }),
    env: { PATH_STORE: 'intervals' },
  })
  expect([response.status, await response.json()]).toEqual([
    200, { date: '2026-10-21T1801', stats: { [prefix]: { bytes: 90, objects: 9 } }, groups: 1 },
  ])
})

it('answers an index error as a 503 that is never stored', async () => {
  prefixesAt.mockImplementation(async () => { throw new Error('scan 2026-10-21T1801 is not in interval store generation current-gen and reads per-scan') })
  const response = await onRequestPost({
    request: new Request('http://localhost/api/prefixes', {
      method: 'POST', body: JSON.stringify({ date: '2026-10-21T1801', prefixes: ['s3://bucket/users/kept/'] }),
    }),
    env: { PATH_STORE: 'intervals' },
  })
  expect([response.status, response.headers.get('cache-control'), await response.json()]).toEqual([
    503, 'no-store', { error: 'scan 2026-10-21T1801 is not in interval store generation current-gen and reads per-scan' },
  ])
})
