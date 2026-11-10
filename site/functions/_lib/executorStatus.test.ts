import { afterEach, expect, it, vi } from 'vitest'
import { onRequestGet } from '../api/executor-status'
import { requireAdmin } from './auth'
import { sqliteD1 } from './testD1'

vi.mock('./auth', async importOriginal => ({
  ...await importOriginal<typeof import('./auth')>(),
  requireAdmin: vi.fn(),
}))
afterEach(() => vi.restoreAllMocks())

it('gates agent metadata and returns fresh, uncached readiness only for laptop deployments', async () => {
  const { db } = await sqliteD1()
  const now = Date.UTC(2026, 10, 10, 12, 0) / 1000
  vi.spyOn(Date, 'now').mockReturnValue(now * 1000)
  const ctx = { request: new Request('https://disk.example/api/executor-status'), env: { DB: db, EXECUTOR: 'laptop' } }
  vi.mocked(requireAdmin).mockResolvedValueOnce(new Response('forbidden', { status: 403 }))
  const denied = await onRequestGet(ctx)
  expect([denied.status, await denied.text()]).toEqual([403, 'forbidden'])
  vi.mocked(requireAdmin).mockResolvedValue({ email: 'admin@example.test', name: null, admin: true, scopes: ['admin'], via: 'session', subject: null })
  await db.prepare("INSERT INTO agents (name, seen_ts) VALUES ('drainer', ?)").bind(now).run()
  const ready = await onRequestGet(ctx)
  const data = await ready.json() as Record<string, unknown>
  expect({ status: ready.status, cache: ready.headers.get('cache-control'), ...data }).toEqual({
    status: 200, cache: 'no-store', executor: 'laptop', ready: true, last_seen: now, checked_at: now, stale_after: 180,
  })
  const other = await onRequestGet({ ...ctx, env: { DB: db, EXECUTOR: 'sweep' } })
  expect([other.status, await other.json()]).toEqual([404, { error: 'This executor has no laptop cleanup agent.' }])
})
