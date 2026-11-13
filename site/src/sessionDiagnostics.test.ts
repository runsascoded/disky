/** A failed fetch's browser context and `life` events through disky's own pieces: its session-log route (gate,
 *  activity-kind filter, D1 store), its activity feed's tally, and the timeline `/admin/sessions` renders. The
 *  client that records them is the auth package's, tested there. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { onRequest } from '../functions/api/session-log/[[path]]'
import { gateFor } from '../functions/_lib/auth'
import { mergeKinds } from '../functions/_lib/activityShape'
import type { StoredEvent } from '../functions/_lib/sessionLogShape'
import { sqliteD1 } from '../functions/_lib/testD1'
import { timeline } from './sessionsModel'

const NOW = Date.UTC(2026, 10, 9, 23, 4)
const T0 = NOW - 10_000
const HOST = 'https://site.example.test'
const SID = 'sid-diagnostics'

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
afterEach(() => { vi.useRealTimers() })

/** One page load's events, as the package's client sends them (and, for one failure, as clients before `on` /
 *  `vis` did). */
const EVENTS = [
  { t: T0, k: 'start', u: '/marin-na' },
  { t: T0, k: 'life', s: 'init', on: true, vis: 'visible', discarded: true },
  // Failed while the browser went offline and the tab hidden: an offline failure, not an error.
  { t: T0 + 1000, k: 'api', id: 1, on0: true, vis0: 'visible', visMs: 1000, u: '/api/series?path=marin-na', end: T0 + 3400, wall: 2400, on: false, vis: 'hidden', s: 0, ms: 2400, err: 'TypeError: Failed to fetch' },
  { t: T0 + 5000, k: 'life', s: 'gap', on: true, vis: 'visible', ms: 48 * 60_000 },
  { t: T0 + 5000, k: 'api', id: 2, on0: true, vis0: 'visible', visMs: 0, gapMs: 48 * 60_000, gapAgo: 0, u: '/api/series?path=marin-na', end: T0 + 5030, wall: 30, on: true, vis: 'visible', s: 200, ms: 30, rt: 'default', ray: '1234567890abcdef-EWR' },
  // An older client's offline failure: `online` / `visibility` / `visible_ms`, still accepted and still read.
  { t: T0 + 6000, k: 'api', u: '/api/read', s: 0, ms: 12, err: 'TypeError: Failed to fetch', online: false, visibility: 'visible', visible_ms: 4000 },
  // Online and visible: a real failure.
  { t: T0 + 7000, k: 'api', id: 3, on0: true, vis0: 'visible', visMs: 7000, u: '/api/other', end: T0 + 7005, wall: 5, on: true, vis: 'visible', s: 0, ms: 5, err: 'TypeError: Failed to fetch' },
]

async function setup(kinds: { kind: string; log: number; notify: string }[] = []) {
  const { db, raw } = await sqliteD1()
  for (const k of kinds) raw.prepare('INSERT INTO activity_kinds (kind, log, notify, who, ts) VALUES (?, ?, ?, ?, ?)').run(k.kind, k.log, k.notify, 'test', NOW / 1000)
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  const gate = gateFor(env as never)!
  const mint = async (email: string, scopes: string[]) => (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const viewer = await mint('ann@example.test', ['cw'])
  const admin = await mint('ops@example.test', ['cw', 'admin'])
  const pending: Promise<unknown>[] = []
  const call = async (method: string, path: string, token: string, body?: unknown) => {
    const request = new Request(`${HOST}/api/session-log${path}`, {
      method, headers: { authorization: `Bearer ${token}`, 'user-agent': 'UA/1' }, ...(body ? { body: JSON.stringify(body) } : {}),
    })
    const res = await onRequest({ request, env, waitUntil: (p: Promise<unknown>) => { pending.push(p) } } as never)
    const text = await res.text()
    return [res.status, text ? JSON.parse(text) : null] as const
  }
  expect((await call('PUT', '/switch', admin, { enabled: true, until: '2026-11-12' }))[0]).toBe(200)
  const post = async () => {
    const res = await call('POST', '', viewer, { sid: SID, pid: 'pid-diagnostics', seq: 0, build: 'abc1234567', events: EVENTS })
    await Promise.all(pending)
    return res
  }
  const read = async () => (await call('GET', `/sessions/${SID}`, admin))[1] as { session: { n_events: number; n_errors: number }; events: StoredEvent[] }
  const counts = () => raw.prepare('SELECT counts FROM activity_bursts').all().map(r => JSON.parse(String(r.counts)))
  return { post, read, counts }
}

it('stores every diagnostic field and `life` event, counts only the online, visible failure as an error, and renders the context with the later success', async () => {
  const s = await setup()
  expect(await s.post()).toEqual([204, null])
  const { session, events } = await s.read()
  expect({ n_events: session.n_events, n_errors: session.n_errors }).toEqual({ n_events: 7, n_errors: 1 })
  expect(events).toEqual(EVENTS.map(e => ({ ...e, pid: 'pid-diagnostics', seq: 0, _received_ms: NOW })))
  expect(timeline(events).map(({ text, error, lane }) => ({ text, error, lane }))).toEqual([
    { text: 'page load /marin-na', error: false, lane: 'nav' },
    { text: 'browser init · online hint · visible · previously discarded', error: false, lane: 'page' },
    { text: 'GET /api/series?path=marin-na → failed · 2400ms · TypeError: Failed to fetch · request #1 · online hint → offline hint · visible → hidden · 1s since visible', error: false, lane: 'api' },
    { text: 'browser gap 48m 00s · online hint · visible', error: false, lane: 'page' },
    { text: 'GET /api/series?path=marin-na → 200 · 30ms · request #2 · Ray 1234567890abcdef-EWR · later success after request #1 (1s after failure)', error: false, lane: 'api' },
    { text: 'GET /api/read → failed · 12ms · TypeError: Failed to fetch', error: false, lane: 'api' },
    { text: 'GET /api/other → failed · 5ms · TypeError: Failed to fetch · request #3 · online hint · visible · 7s since visible', error: true, lane: 'error' },
  ])
  // The activity feed: `life` is its own kind, recorded but never reported by default; offline failures are requests.
  expect(s.counts()).toEqual([{ view: 1, life: 2, api: 3, error: 1 }])
  expect(mergeKinds([]).life).toEqual({ log: true, notify: 'off' })
})

it('drops `life` events when an admin turns the kind\'s recording off', async () => {
  const s = await setup([{ kind: 'life', log: 0, notify: 'off' }])
  expect(await s.post()).toEqual([204, null])
  const { session, events } = await s.read()
  expect({ n_events: session.n_events, kinds: events.map(e => e.k) }).toEqual({
    n_events: 5, kinds: ['start', 'api', 'api', 'api', 'api'],
  })
  expect(s.counts()).toEqual([{ view: 1, api: 3, error: 1 }])
})
