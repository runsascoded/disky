// `/api/sweep/soft-delete`: any viewer reads the buckets' soft delete as of the job's newest reading; null where none is
// written, and on an executor whose undo isn't a soft-delete restore.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'
import { fakeR2, softDeleteDoc } from '../../_lib/testR2'
import { onRequestGet } from './soft-delete'

const FILES = {
  'snapshots/soft-delete/2026-11-04.json': softDeleteDoc('2026-11-04', { a: false, b: false }),
  'snapshots/soft-delete/2026-11-05.json': softDeleteDoc('2026-11-05', { a: true, b: false }),
}
let env: Record<string, unknown>
let viewer: string

beforeAll(async () => {
  const { db } = await sqliteD1()
  env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs', EXECUTOR: 'sweep', SERVED_R2: '1', INDEX_R2: fakeR2(FILES) }
  viewer = (await gateFor(env as never)!.mint({ email: 'ann@example.test', scopes: ['gcs:read'], name: 'ann', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
})

const get = async (token?: string, over: Record<string, unknown> = {}) => {
  const res = await onRequestGet({
    request: new Request('https://site.example.test/api/sweep/soft-delete', { headers: token ? { authorization: `Bearer ${token}` } : {} }),
    env: { ...env, ...over },
  } as never)
  return [res.status, await res.json(), res.headers.get('cache-control')] as const
}

describe('/api/sweep/soft-delete', () => {
  it('a read-only viewer reads the newest reading\'s buckets, and when it was read', async () => {
    expect(await get(viewer)).toEqual([200, { soft_delete: { asOf: '2026-11-05 07:12Z', off: { a: true, b: false } } }, 'private, max-age=300'])
  })
  it('null with no reading, and on `plan-sweep` (its undo is its own hold: nothing of it is read)', async () => {
    expect(await get(viewer, { INDEX_R2: fakeR2({}) })).toEqual([200, { soft_delete: null }, 'private, max-age=300'])
    expect(await get(viewer, { EXECUTOR: 'plan-sweep' })).toEqual([200, { soft_delete: null }, 'private, max-age=300'])
  })
  it('viewers only', async () => {
    expect((await get())[0]).toBe(401)
  })
})
