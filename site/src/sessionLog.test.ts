/** Public logger policy against disky's real gate and shared route: logging remains a runtime admin switch. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { shouldLog } from '@open-athena/auth/session-log/client'
import type { SessionLogConfig } from '@open-athena/auth/session-log'
import { onRequest } from '../functions/api/session-log/[[path]]'
import { gateFor } from '../functions/_lib/auth'
import { sqliteD1 } from '../functions/_lib/testD1'

const NOW = Date.UTC(2026, 9, 12, 15)
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
afterEach(() => { vi.useRealTimers() })

it('uses the shared client policy with the real admin switch: off, dated, off, indefinite', async () => {
  const { db } = await sqliteD1()
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  const gate = gateFor(env as never)!
  const viewer = (await gate.mint({ email: 'ann@example.test', scopes: ['cw'], name: 'ann', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const admin = (await gate.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const read = async () => {
    const response = await onRequest({ request: new Request('https://site.example.test/api/session-log', { headers: { authorization: `Bearer ${viewer}` } }), env } as never)
    const config = await response.json() as SessionLogConfig
    return { status: response.status, logs: shouldLog(config), until: config.until, appEvents: config.appEvents }
  }
  const put = async (body: unknown) => {
    const response = await onRequest({ request: new Request('https://site.example.test/api/session-log/switch', { method: 'PUT', headers: { authorization: `Bearer ${admin}` }, body: JSON.stringify(body) }), env } as never)
    expect(response.status).toBe(200)
  }
  const seen = [await read()]
  await put({ enabled: true, until: '2026-10-14' }); seen.push(await read())
  await put({ enabled: false }); seen.push(await read())
  await put({ enabled: true, indefinite: true }); seen.push(await read())
  expect(seen).toEqual([
    { status: 200, logs: false, until: null, appEvents: {} },
    { status: 200, logs: true, until: Date.UTC(2026, 9, 15), appEvents: {} },
    { status: 200, logs: false, until: null, appEvents: {} },
    { status: 200, logs: true, until: Date.UTC(2100, 0, 1), appEvents: {} },
  ])
})
