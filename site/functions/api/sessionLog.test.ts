// `/api/session-log` (specs/session-log.md): the admin switch (gate, default /
// cap / expiry, the per-isolate cache, the audit row), auth, validation and
// size limits, storage writes, retention and the admin reads — over the D1
// schema (`session_log*` in `site/schema.sql`) and the real gate (Bearer
// grants minted in the same in-memory D1).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onRequest } from './session-log/[[path]]'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { d1SessionLogStore } from '@open-athena/auth/session-log/d1'
import type { SessionFilter } from '@open-athena/auth/session-log'
const listSessions = (db: D1Database, filter: SessionFilter) => d1SessionLogStore(db).listSessions(filter)
const purge = (db: D1Database, cutoff: number) => d1SessionLogStore(db).purge(cutoff)
import { latency } from '../_lib/sessionLatency'
import { FOREVER_MS, fmtCursor, isForever, LIMITS, parseCursor, parseUntil, type SessionRow, switchState, validateBatch } from '../_lib/sessionLogShape'

const NOW = Date.UTC(2026, 9, 12, 15, 0, 0)  // 2026-10-12T15:00Z
const HOST = 'https://site.example.test'

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
afterEach(() => { vi.useRealTimers() })

describe('D1', () => {
  const TTL = 60_000

  /** A fresh deployment (switch off), or with the switch on through `on` (`PUT` by the admin; its audit row
   *  dropped). Each setup's D1 has its own switch cache, as each isolate does. */
  async function setup(vars: Record<string, string> = {}, on: string | null = '2026-10-17') {
    const { db, raw } = await sqliteD1()
    const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', ...vars }
    const gate = gateFor(env as never)!
    const grantIds: Record<string, string> = {}
    const tok = async (email: string, scopes: string[]) => {
      const { token, grant } = await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })
      grantIds[email] = grant.id
      return token
    }
    const viewer = await tok('ann@example.test', ['cw'])
    const adminT = await tok('ops@example.test', ['cw', 'admin'])
    const call = async (method: string, path: string, opts: { token?: string; body?: string; headers?: Record<string, string> } = {}) => {
      const url = new URL(`${HOST}/api/session-log${path}`)
      const rest = url.pathname.replace(/^\/api\/session-log\/?/, '')
      const request = new Request(url, {
        method,
        body: opts.body,
        headers: { ...(opts.token ? { authorization: `Bearer ${opts.token}` } : {}), 'user-agent': 'UA/1', ...opts.headers },
      })
      const res = await onRequest({ request, env, params: rest ? { path: rest.split('/') } : {} } as never)
      const text = await res.text()
      return [res.status, text ? JSON.parse(text) : null] as const
    }
    if (on !== null) {
      const [st] = await call('PUT', '/switch', { token: adminT, body: JSON.stringify({ enabled: true, until: on }) })
      if (st !== 200) throw new Error(`switch PUT: ${st}`)
      raw.exec('DELETE FROM admin_edits')
    }
    const put = (body: unknown, token = adminT) => call('PUT', '/switch', { token, body: JSON.stringify(body) })
    return { db, raw, env, call, put, viewer, adminT, gate, grantIds }
  }

  const ev = (t: number, k: string, f: Record<string, unknown> = {}) => ({ t, k, ...f })
  const batch = (o: Partial<{ sid: string; pid: string; seq: number; build: string; events: unknown[] }> = {}) => JSON.stringify({
    sid: 'sid-aaaaaa', pid: 'pid-111111', seq: 0, build: 'abc1234567',
    events: [ev(NOW - 5000, 'start', { u: '/?d=2026-10-10' }), ev(NOW - 4000, 'click', { n: 'scan', l: 'scan' })],
    ...o,
  })

  describe('parseUntil (an admin\'s end date) and switchState', () => {
    it('empty = 7 days; a date runs through that UTC day; an instant ends at it; past, past the 31-day cap and bad values are refused', () => {
      expect([
        parseUntil(undefined, NOW),
        parseUntil('', NOW),
        parseUntil('2026-10-17', NOW),
        parseUntil('2026-10-12T16:00:00Z', NOW),
        parseUntil('2026-10-12', NOW),
        parseUntil('2026-11-12', NOW),
        parseUntil('2026-10-11', NOW),
        parseUntil('2026-10-12T15:00:00Z', NOW),
        parseUntil('2026-11-13', NOW),
        parseUntil('2027-10-17', NOW),
        parseUntil('soon', NOW),
      ]).toEqual([
        { until: Date.UTC(2026, 9, 20) },
        { until: Date.UTC(2026, 9, 20) },
        { until: Date.UTC(2026, 9, 18) },
        { until: Date.UTC(2026, 9, 12, 16) },
        { until: Date.UTC(2026, 9, 13) },
        { until: Date.UTC(2026, 10, 13) },
        { error: 'until 2026-10-11 is in the past' },
        { error: 'until 2026-10-12T15:00:00Z is in the past' },
        { error: 'until 2026-11-13 is more than 31 days ahead (latest: 2026-11-12)' },
        { error: 'until 2027-10-17 is more than 31 days ahead (latest: 2026-11-12)' },
        { error: 'unparseable until: soon' },
      ])
    })
    it('the stored end: null = off; at or past it = expired', () => {
      expect([
        switchState(null, NOW),
        switchState(NOW, NOW),
        switchState(NOW + 1, NOW),
      ]).toEqual([
        { until: null, reason: 'off' },
        { until: NOW, reason: 'expired' },
        { until: NOW + 1, reason: null },
      ])
    })
  })

  describe('validateBatch', () => {
    const ok = JSON.parse(batch())
    it('accepts a well-formed batch as is', () => {
      expect(validateBatch(ok)).toEqual({ batch: ok })
    })
    it('refuses bad ids, kinds, fields, and oversize batches / events', () => {
      const big = Array.from({ length: LIMITS.batchEvents + 1 }, (_, i) => ev(NOW + i, 'vis', { s: 'visible' }))
      expect([
        validateBatch([]),
        validateBatch({ ...ok, sid: 'x' }),
        validateBatch({ ...ok, pid: 'has space' }),
        validateBatch({ ...ok, seq: -1 }),
        validateBatch({ ...ok, events: [] }),
        validateBatch({ ...ok, events: [ev(NOW, 'keystroke')] }),
        validateBatch({ ...ok, events: [{ k: 'nav' }] }),
        validateBatch({ ...ok, events: [ev(NOW, 'nav', { u: ['a'] })] }),
        validateBatch({ ...ok, events: [ev(NOW, 'api', { b: { items: { deep: 1 } } })] }),
        validateBatch({ ...ok, events: big }),
        validateBatch({ ...ok, events: [ev(NOW, 'error', { st: 'x'.repeat(LIMITS.eventBytes) })] }),
      ]).toEqual([
        { status: 400, error: 'body must be a JSON object' },
        { status: 400, error: 'bad sid' },
        { status: 400, error: 'bad pid' },
        { status: 400, error: 'bad seq' },
        { status: 400, error: 'events must be a non-empty array' },
        { status: 400, error: 'event 0: unknown kind keystroke' },
        { status: 400, error: 'event 0: bad t' },
        { status: 400, error: 'event 0: bad field u' },
        { status: 400, error: 'event 0: bad field b' },
        { status: 413, error: 'more than 300 events' },
        { status: 413, error: 'event 0: over 4096 bytes' },
      ])
    })
  })

  describe('the switch: GET / PUT /api/session-log/switch', () => {
    const bounds = { defaultUntil: Date.UTC(2026, 9, 20), maxUntil: Date.UTC(2026, 10, 13) }
    const nowS = NOW / 1000

    it('admins only: signed out 401, a viewer 403 (and a refused PUT changes nothing)', async () => {
      const s = await setup({}, null)
      expect([
        await s.call('GET', '/switch'),
        await s.call('GET', '/switch', { token: s.viewer }),
        await s.put({ enabled: true }, s.viewer),
        await s.call('DELETE', '/switch', { token: s.adminT }),
      ]).toEqual([
        [401, { error: 'unauthenticated' }],
        [403, { error: 'forbidden' }],
        [403, { error: 'forbidden' }],
        [405, { error: 'method not allowed' }],
      ])
      expect(s.raw.prepare('SELECT * FROM session_log_switch').all()).toEqual([])
    })

    it('off by default; on with no date = 7 days; a date; off again — each change audited in admin_edits', async () => {
      const s = await setup({}, null)
      const on7 = { enabled: true, until: Date.UTC(2026, 9, 20), indefinite: false, reason: null, who: 'ops@example.test', ts: nowS, ...bounds }
      expect([
        await s.call('GET', '/switch', { token: s.adminT }),
        await s.put({ enabled: true }),
        await s.put({ enabled: true, until: '2026-10-14' }),
        await s.put({ enabled: false, until: '2026-10-14' }),
        await s.call('GET', '/switch', { token: s.adminT }),
      ]).toEqual([
        [200, { enabled: false, until: null, indefinite: false, reason: 'off', who: null, ts: null, ...bounds }],
        [200, on7],
        [200, { ...on7, until: Date.UTC(2026, 9, 15) }],
        [200, { enabled: false, until: null, indefinite: false, reason: 'off', who: 'ops@example.test', ts: nowS, ...bounds }],
        [200, { enabled: false, until: null, indefinite: false, reason: 'off', who: 'ops@example.test', ts: nowS, ...bounds }],
      ])
      expect(s.raw.prepare('SELECT * FROM session_log_switch').all()).toEqual([{ id: 1, until_ms: null, who: 'ops@example.test', ts: nowS }])
      const row = (until: number | null) => JSON.stringify({ until_ms: until, who: 'ops@example.test', ts: nowS })
      expect(s.raw.prepare("SELECT tbl, pk, action, who, ts, old_json, new_json FROM admin_edits WHERE tbl = 'session_log_switch' ORDER BY id").all()).toEqual([
        { tbl: 'session_log_switch', pk: '1', action: 'insert', who: 'ops@example.test', ts: nowS, old_json: null, new_json: row(Date.UTC(2026, 9, 20)) },
        { tbl: 'session_log_switch', pk: '1', action: 'update', who: 'ops@example.test', ts: nowS, old_json: row(Date.UTC(2026, 9, 20)), new_json: row(Date.UTC(2026, 9, 15)) },
        { tbl: 'session_log_switch', pk: '1', action: 'update', who: 'ops@example.test', ts: nowS, old_json: row(Date.UTC(2026, 9, 15)), new_json: row(null) },
      ])
    })

    it('refuses past the 31-day cap, past dates and bad bodies (nothing stored)', async () => {
      const s = await setup({}, null)
      expect([
        await s.put({ enabled: true, until: '2026-11-13' }),
        await s.put({ enabled: true, until: '2026-10-01' }),
        await s.put({ enabled: 'yes' }),
        await s.put({ enabled: true, until: 20261017 }),
        await s.call('PUT', '/switch', { token: s.adminT, body: '{nope' }),
      ]).toEqual([
        [400, { error: 'until 2026-11-13 is more than 31 days ahead (latest: 2026-11-12)' }],
        [400, { error: 'until 2026-10-01 is in the past' }],
        [400, { error: 'enabled must be true or false' }],
        [400, { error: 'until must be a date string' }],
        [400, { error: 'body is not JSON' }],
      ])
      expect(s.raw.prepare('SELECT * FROM session_log_switch').all()).toEqual([])
    })

    it('expires by itself: past its end the switch reads off (expired), with no write', async () => {
      const s = await setup({}, '2026-10-12')
      vi.setSystemTime(Date.UTC(2026, 9, 13))
      expect(await s.call('GET', '/switch', { token: s.adminT })).toEqual([200, {
        enabled: false, until: Date.UTC(2026, 9, 13), indefinite: false, reason: 'expired', who: 'ops@example.test', ts: NOW / 1000,
        defaultUntil: Date.UTC(2026, 9, 21), maxUntil: Date.UTC(2026, 10, 14),
      }])
    })

    it('without the migration: reads "not set up", a PUT is 503', async () => {
      const s = await setup({}, null)
      s.raw.exec('DROP TABLE session_log_switch')
      expect([
        await s.call('GET', '/switch', { token: s.adminT }),
        await s.put({ enabled: true }),
      ]).toEqual([
        [200, { enabled: false, until: null, indefinite: false, reason: 'not set up', who: null, ts: null, ...bounds }],
        [503, { error: 'session log is not set up on this deployment (no session_log table)' }],
      ])
    })
  })

  describe('no end date (indefinite)', () => {
    const bounds = { defaultUntil: Date.UTC(2026, 9, 20), maxUntil: Date.UTC(2026, 10, 13) }
    const nowS = NOW / 1000
    const HAND_2100 = 4102444800000  // what was written to prod by hand: 2100-01-01T00:00Z
    const onForever = (until: number, who = 'ops@example.test', ts: number | null = nowS) =>
      ({ enabled: true, until, indefinite: true, reason: null, who, ts, ...bounds })

    it('the sentinel is 2100-01-01T00:00Z; at or past it reads as indefinite, before it as a date', () => {
      expect(FOREVER_MS).toBe(HAND_2100)
      expect([null, NOW, Date.UTC(2099, 11, 31), FOREVER_MS, Date.UTC(2200, 0, 1)].map(isForever)).toEqual([false, false, false, true, true])
    })

    it('PUT {enabled, indefinite}: stores FOREVER_MS (audited), reads on with no end; to a date, back, and off', async () => {
      const s = await setup({}, null)
      expect([
        await s.put({ enabled: true, indefinite: true }),
        await s.call('GET', '/switch', { token: s.adminT }),
        await s.put({ enabled: true, until: '2026-10-14' }),
        await s.put({ enabled: true, indefinite: true, until: '' }),
        await s.put({ enabled: false }),
      ]).toEqual([
        [200, onForever(FOREVER_MS)],
        [200, onForever(FOREVER_MS)],
        [200, { enabled: true, until: Date.UTC(2026, 9, 15), indefinite: false, reason: null, who: 'ops@example.test', ts: nowS, ...bounds }],
        [200, onForever(FOREVER_MS)],
        [200, { enabled: false, until: null, indefinite: false, reason: 'off', who: 'ops@example.test', ts: nowS, ...bounds }],
      ])
      const row = (until: number | null) => JSON.stringify({ until_ms: until, who: 'ops@example.test', ts: nowS })
      expect(s.raw.prepare("SELECT action, old_json, new_json FROM admin_edits WHERE tbl = 'session_log_switch' ORDER BY id").all()).toEqual([
        { action: 'insert', old_json: null, new_json: row(FOREVER_MS) },
        { action: 'update', old_json: row(FOREVER_MS), new_json: row(Date.UTC(2026, 9, 15)) },
        { action: 'update', old_json: row(Date.UTC(2026, 9, 15)), new_json: row(FOREVER_MS) },
        { action: 'update', old_json: row(FOREVER_MS), new_json: row(null) },
      ])
    })

    it('refuses an indefinite with a date, with enabled: false, or not a boolean (nothing stored)', async () => {
      const s = await setup({}, null)
      expect([
        await s.put({ enabled: true, indefinite: true, until: '2026-10-14' }),
        await s.put({ enabled: false, indefinite: true }),
        await s.put({ enabled: true, indefinite: 'yes' }),
      ]).toEqual([
        [400, { error: 'until and indefinite are exclusive' }],
        [400, { error: 'indefinite needs enabled: true' }],
        [400, { error: 'indefinite must be true or false' }],
      ])
      expect(s.raw.prepare('SELECT * FROM session_log_switch').all()).toEqual([])
    })

    it('the hand-written 2100 row (and any later one) reads as on, no end date; the boot config is on, years later too', async () => {
      const s = await setup({}, null)
      s.raw.prepare('INSERT INTO session_log_switch VALUES (1, ?, ?, ?)').run(HAND_2100, 'ryan', 1_760_000_000)
      expect([
        await s.call('GET', '/switch', { token: s.adminT }),
        await s.call('GET', '', { token: s.viewer }),
      ]).toEqual([
        [200, onForever(HAND_2100, 'ryan', 1_760_000_000)],
        [200, { enabled: true, until: HAND_2100, reason: null, on: true, retainDays: 30, appEvents: {} }],
      ])
      s.raw.prepare('UPDATE session_log_switch SET until_ms = ?').run(Date.UTC(2200, 0, 1))
      vi.setSystemTime(Date.UTC(2031, 0, 1))
      expect(await s.call('GET', '', { token: s.viewer })).toEqual([200, { enabled: true, until: Date.UTC(2200, 0, 1), reason: null, on: true, retainDays: 30, appEvents: {} }])
    })
  })

  describe('session list pages (keyset on last_ms desc, sid)', () => {
    /** Sessions `s00`…: `last_ms` = NOW − i·1000, but `s04`–`s06` share one `last_ms` (ties order by sid); odd ones
     *  have errors; even ones are bob's. */
    async function seeded(n: number) {
      const s = await setup({}, null)
      const ins = s.raw.prepare('INSERT INTO session_log (sid, email, via, build, ua, started_ms, last_ms, n_events, n_errors, n_batches, first_url, received_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
      for (let i = 0; i < n; i++) {
        const last = i >= 4 && i <= 6 ? NOW - 4000 : NOW - i * 1000
        ins.run(`sid-s${String(i).padStart(4, '0')}`, i % 2 ? 'ann@example.test' : 'bob@example.test', 'session', 'b', null, last - 500, last, 1, i % 2, 1, null, NOW / 1000)
      }
      return s
    }
    const sids = (r: SessionRow[]) => r.map(x => x.sid.slice(5))
    const page = (r: { sessions: SessionRow[]; total: number; offset: number; prev: string | null; next: string | null }) =>
      ({ sids: sids(r.sessions), total: r.total, offset: r.offset, prev: r.prev, next: r.next })
    const cur = (i: number, last = NOW - i * 1000) => `${last}.sid-s${String(i).padStart(4, '0')}`

    it('cursors: `<last_ms>.<sid>` round-trips; anything else is refused', () => {
      expect([
        fmtCursor({ last_ms: NOW, sid: 'sid-aaaaaa' }),
        parseCursor(`${NOW}.sid-aaaaaa`),
        parseCursor('sid-aaaaaa'),
        parseCursor(`${NOW}.sid`),
        parseCursor(`-1.sid-aaaaaa`),
        parseCursor(`${NOW}.sid-aa.aaaa`),
      ]).toEqual([`${NOW}.sid-aaaaaa`, { lastMs: NOW, sid: 'sid-aaaaaa' }, null, null, null, null])
    })

    it('walks forward and back by 3s, ties in sid order, counts exact; a short "newer" page is answered as the first', async () => {
      const { db } = await seeded(8)
      const p1 = await listSessions(db, { limit: 3 })
      const p2 = await listSessions(db, { limit: 3, after: parseCursor(p1.next!)! })
      const p3 = await listSessions(db, { limit: 3, after: parseCursor(p2.next!)! })
      expect([page(p1), page(p2), page(p3)]).toEqual([
        { sids: ['0000', '0001', '0002'], total: 8, offset: 0, prev: null, next: cur(2) },
        { sids: ['0003', '0004', '0005'], total: 8, offset: 3, prev: cur(3), next: cur(5, NOW - 4000) },
        { sids: ['0006', '0007'], total: 8, offset: 6, prev: cur(6, NOW - 4000), next: null },
      ])
      expect([
        page(await listSessions(db, { limit: 3, before: parseCursor(p3.prev!)! })),
        page(await listSessions(db, { limit: 3, before: parseCursor(p2.prev!)! })),
        // Two rows ahead of s0002: short, so the first page instead.
        page(await listSessions(db, { limit: 3, before: parseCursor(cur(2))! })),
        // Past the end: empty, at the end.
        page(await listSessions(db, { limit: 3, after: parseCursor(cur(7))! })),
      ]).toEqual([
        { sids: ['0003', '0004', '0005'], total: 8, offset: 3, prev: cur(3), next: cur(5, NOW - 4000) },
        { sids: ['0000', '0001', '0002'], total: 8, offset: 0, prev: null, next: cur(2) },
        { sids: ['0000', '0001', '0002'], total: 8, offset: 0, prev: null, next: cur(2) },
        { sids: [], total: 8, offset: 8, prev: null, next: null },
      ])
    })

    it('filters apply to the page and its counts; the default page is 50', async () => {
      const { db } = await seeded(120)
      const errs = await listSessions(db, { errors: true, limit: 2 })
      const bob = await listSessions(db, { email: 'BOB', since: NOW - 5000, limit: 2 })
      expect([
        page(errs),
        page(await listSessions(db, { errors: true, limit: 2, after: parseCursor(errs.next!)! })),
        page(bob),
        page(await listSessions(db, { email: 'BOB', since: NOW - 5000, limit: 2, after: parseCursor(bob.next!)! })),
      ]).toEqual([
        { sids: ['0001', '0003'], total: 60, offset: 0, prev: null, next: cur(3) },
        { sids: ['0005', '0007'], total: 60, offset: 2, prev: cur(5, NOW - 4000), next: cur(7) },
        { sids: ['0000', '0002'], total: 4, offset: 0, prev: null, next: cur(2) },
        { sids: ['0004', '0006'], total: 4, offset: 2, prev: cur(4, NOW - 4000), next: null },
      ])
      const first = await listSessions(db, {})
      expect([first.sessions.length, first.total, first.offset, first.next]).toEqual([50, 120, 0, cur(49)])
    })

    it('GET /sessions: after= / before= pages; both, or a bad cursor, is 400', async () => {
      const s = await seeded(5)
      const get = async (q: string) => { const [st, b] = await s.call('GET', `/sessions${q}`, { token: s.adminT }); return st === 200 ? page(b) : [st, b] }
      expect([
        await get('?limit=2'),
        await get(`?limit=2&after=${cur(1)}`),
        await get(`?limit=2&before=${cur(4, NOW - 4000)}`),
        await get(`?after=${cur(1)}&before=${cur(1)}`),
        await get('?after=nope'),
      ]).toEqual([
        { sids: ['0000', '0001'], total: 5, offset: 0, prev: null, next: cur(1) },
        { sids: ['0002', '0003'], total: 5, offset: 2, prev: cur(2), next: cur(3) },
        { sids: ['0002', '0003'], total: 5, offset: 2, prev: cur(2), next: cur(3) },
        [400, { error: 'after and before are exclusive' }],
        [400, { error: 'bad cursor: nope' }],
      ])
    })
  })

  describe('GET /api/session-log (the boot config)', () => {
    it('off unless switched on, off once expired, on only for a viewer', async () => {
      const off = await setup({}, null)
      const on = await setup()
      expect([
        await off.call('GET', ''),
        await off.call('GET', '', { token: off.viewer }),
        await on.call('GET', ''),
        await on.call('GET', '', { token: on.viewer }),
      ]).toEqual([
        [200, { enabled: false, until: null, reason: 'off', on: false, retainDays: 30, appEvents: {} }],
        [200, { enabled: false, until: null, reason: 'off', on: false, retainDays: 30, appEvents: {} }],
        [200, { enabled: false, until: Date.UTC(2026, 9, 18), reason: 'no viewer identity', on: true, retainDays: 30, appEvents: {} }],
        [200, { enabled: true, until: Date.UTC(2026, 9, 18), reason: null, on: true, retainDays: 30, appEvents: {} }],
      ])
      vi.setSystemTime(Date.UTC(2026, 9, 18))
      expect(await on.call('GET', '', { token: on.viewer })).toEqual([200, { enabled: false, until: Date.UTC(2026, 9, 18), reason: 'expired', on: false, retainDays: 30, appEvents: {} }])
    })

    it('a PUBLIC_READ deploy logs anonymous viewers', async () => {
      const pub = await setup({ PUBLIC_READ: '1' })
      expect(await pub.call('GET', '')).toEqual([200, { enabled: true, until: Date.UTC(2026, 9, 18), reason: null, on: true, retainDays: 30, appEvents: {} }])
    })

    it(`reads the switch at most once per ${TTL / 1000} s per isolate; an admin change in this isolate applies at once`, async () => {
      const s = await setup({}, null)
      const cfg = async () => (await s.call('GET', '', { token: s.viewer }))[1].enabled
      const seen = [await cfg()]
      // Another isolate turns it on: this one keeps its read until the TTL passes.
      s.raw.exec(`INSERT INTO session_log_switch (id, until_ms, who, ts) VALUES (1, ${Date.UTC(2026, 9, 18)}, 'x', 0)`)
      seen.push(await cfg())
      vi.setSystemTime(NOW + TTL - 1)
      seen.push(await cfg())
      vi.setSystemTime(NOW + TTL)
      seen.push(await cfg())
      // Off, through this isolate's admin route: no wait.
      await s.put({ enabled: false })
      seen.push(await cfg())
      expect(seen).toEqual([false, false, false, true, false])
    })

    it('a cached read still expires on the instant (the cache holds the end, not the answer)', async () => {
      const s = await setup({}, new Date(NOW + 30_000).toISOString())
      const cfg = async () => (await s.call('GET', '', { token: s.viewer }))[1]
      const seen = [await cfg()]
      vi.setSystemTime(NOW + 30_000)
      seen.push(await cfg())
      expect(seen).toEqual([
        { enabled: true, until: NOW + 30_000, reason: null, on: true, retainDays: 30, appEvents: {} },
        { enabled: false, until: NOW + 30_000, reason: 'expired', on: false, retainDays: 30, appEvents: {} },
      ])
    })

    it('without the migration: off ("not set up")', async () => {
      const s = await setup({}, null)
      s.raw.exec('DROP TABLE session_log_switch')
      expect(await s.call('GET', '', { token: s.viewer })).toEqual([200, { enabled: false, until: null, reason: 'not set up', on: false, retainDays: 30, appEvents: {} }])
    })
  })

  describe('POST /api/session-log', () => {
    it('stores a batch under the request identity and summarizes the session', async () => {
      const { call, viewer, raw, grantIds } = await setup()
      expect(await call('POST', '', { token: viewer, body: batch() })).toEqual([204, null])
      expect(await call('POST', '', {
        token: viewer,
        body: batch({ seq: 1, events: [ev(NOW - 3000, 'api', { u: '/api/subtree', s: 500, ms: 12 }), ev(NOW - 2000, 'error', { msg: 'boom' }), ev(NOW - 1000, 'api', { u: '/api/x', s: 200, ms: 3 })] }),
      })).toEqual([204, null])
      expect(raw.prepare('SELECT * FROM session_log').all()).toEqual([{
        sid: 'sid-aaaaaa', email: 'ann@example.test', via: 'grant', build: 'abc1234567', ua: 'UA/1',
        started_ms: NOW - 5000, last_ms: NOW - 1000, n_events: 5, n_errors: 2, n_batches: 2,
        first_url: '/?d=2026-10-10', received_ts: NOW / 1000, grant_id: grantIds['ann@example.test'], subject_name: null,
      }])
      expect(raw.prepare('SELECT sid, pid, seq, received_ts, first_ms, last_ms, n FROM session_log_batches ORDER BY seq').all()).toEqual([
        { sid: 'sid-aaaaaa', pid: 'pid-111111', seq: 0, received_ts: NOW / 1000, first_ms: NOW - 5000, last_ms: NOW - 4000, n: 2 },
        { sid: 'sid-aaaaaa', pid: 'pid-111111', seq: 1, received_ts: NOW / 1000, first_ms: NOW - 3000, last_ms: NOW - 1000, n: 3 },
      ])
    })

    it('a repeated (sid, pid, seq) — a retried beacon — is ignored and counts nothing', async () => {
      const { call, viewer, raw } = await setup()
      await call('POST', '', { token: viewer, body: batch() })
      expect(await call('POST', '', { token: viewer, body: batch() })).toEqual([204, null])
      expect(raw.prepare('SELECT n_events, n_batches FROM session_log').all()).toEqual([{ n_events: 2, n_batches: 1 }])
    })

    it('refuses: signed out (401), off or expired (503), bad body (400), oversize (413), no tables (503)', async () => {
      const s = await setup()
      const past = await setup({}, '2026-10-12')
      past.raw.exec(`UPDATE session_log_switch SET until_ms = ${NOW - 1}`)
      await past.call('GET', '/switch', { token: past.adminT })  // the admin read refreshes this isolate's cache
      const bare = await setup()
      bare.raw.exec('DROP TABLE session_log; DROP TABLE session_log_batches')
      const huge = JSON.stringify({ pad: 'x'.repeat(LIMITS.bodyBytes) })
      const off = await setup({}, null)
      expect([
        await off.call('POST', '', { token: off.viewer, body: batch() }),
        await s.call('POST', '', { body: batch() }),
        await past.call('POST', '', { token: past.viewer, body: batch() }),
        await s.call('POST', '', { token: s.viewer, body: '{nope' }),
        await s.call('POST', '', { token: s.viewer, body: batch({ sid: '!' }) }),
        await s.call('POST', '', { token: s.viewer, body: huge }),
        await s.call('POST', '', { token: s.viewer, body: batch(), headers: { 'content-length': String(LIMITS.bodyBytes + 1) } }),
        await bare.call('POST', '', { token: bare.viewer, body: batch() }),
        await s.call('PUT', '', { token: s.viewer, body: batch() }),
      ]).toEqual([
        [503, { error: 'session log is off' }],
        [401, { error: 'unauthenticated' }],
        [503, { error: 'session log is off' }],
        [400, { error: 'body is not JSON' }],
        [400, { error: 'bad sid' }],
        [413, { error: 'body over 65536 bytes' }],
        [413, { error: 'body over 65536 bytes' }],
        [503, { error: 'session log is not set up on this deployment (no session_log table)' }],
        [405, { error: 'method not allowed' }],
      ])
      expect(s.raw.prepare('SELECT COUNT(*) AS n FROM session_log_batches').all()).toEqual([{ n: 0 }])
    })

    it(`a session past ${LIMITS.sessionEvents} events stores nothing more (429)`, async () => {
      const { call, viewer, raw } = await setup()
      await call('POST', '', { token: viewer, body: batch() })
      raw.exec(`UPDATE session_log SET n_events = ${LIMITS.sessionEvents - 1}`)
      expect(await call('POST', '', { token: viewer, body: batch({ seq: 1 }) })).toEqual([429, { error: 'session over 20000 events' }])
      expect(raw.prepare('SELECT COUNT(*) AS n FROM session_log_batches').all()).toEqual([{ n: 1 }])
    })
  })

  describe('retention', () => {
    it('purges rows received before the cutoff, from both tables', async () => {
      const { call, viewer, raw, db } = await setup()
      await call('POST', '', { token: viewer, body: batch() })
      await call('POST', '', { token: viewer, body: batch({ sid: 'sid-bbbbbb' }) })
      raw.exec("UPDATE session_log SET received_ts = 100 WHERE sid = 'sid-aaaaaa'; UPDATE session_log_batches SET received_ts = 100 WHERE sid = 'sid-aaaaaa'")
      expect(await purge(db, 1000)).toEqual({ batches: 1, sessions: 1 })
      expect(raw.prepare('SELECT sid FROM session_log').all()).toEqual([{ sid: 'sid-bbbbbb' }])
      expect(raw.prepare('SELECT sid FROM session_log_batches').all()).toEqual([{ sid: 'sid-bbbbbb' }])
    })

    const OLD_AND_NEW = (day: number) => `
      INSERT INTO session_log (sid, started_ms, last_ms, n_events, n_errors, n_batches, received_ts) VALUES
        ('old', 0, 0, 1, 0, 1, ${NOW / 1000 - 8 * day}), ('new', 0, 0, 1, 0, 1, ${NOW / 1000 - 6 * day});
    `

    it('an anonymous GET answers exactly as before and purges nothing, logging off or on', async () => {
      const off = await setup({ SESSION_LOG_RETAIN_DAYS: '7' }, null)
      const on = await setup({ SESSION_LOG_RETAIN_DAYS: '7' })
      for (const s of [off, on]) s.raw.exec(OLD_AND_NEW(86_400))
      expect([await off.call('GET', ''), await on.call('GET', '')]).toEqual([
        [200, { enabled: false, until: null, reason: 'off', on: false, retainDays: 7, appEvents: {} }],
        [200, { enabled: false, until: Date.UTC(2026, 9, 18), reason: 'no viewer identity', on: true, retainDays: 7, appEvents: {} }],
      ])
      expect([
        off.raw.prepare('SELECT sid FROM session_log ORDER BY sid').all(),
        on.raw.prepare('SELECT sid FROM session_log ORDER BY sid').all(),
      ]).toEqual([[{ sid: 'new' }, { sid: 'old' }], [{ sid: 'new' }, { sid: 'old' }]])
    })

    it('an anonymous GET on a PUBLIC_READ deploy: logged, and it purges nothing either', async () => {
      const pub = await setup({ PUBLIC_READ: '1', SESSION_LOG_RETAIN_DAYS: '7' })
      pub.raw.exec(OLD_AND_NEW(86_400))
      expect(await pub.call('GET', '')).toEqual([200, { enabled: true, until: Date.UTC(2026, 9, 18), reason: null, on: true, retainDays: 7, appEvents: {} }])
      expect(pub.raw.prepare('SELECT sid FROM session_log ORDER BY sid').all()).toEqual([{ sid: 'new' }, { sid: 'old' }])
    })

    it('a viewer\'s config GET purges past SESSION_LOG_RETAIN_DAYS — with logging off — at most hourly; a missing table is a nop', async () => {
      const s = await setup({ SESSION_LOG_RETAIN_DAYS: '7' }, null)
      const day = 86_400
      s.raw.exec(OLD_AND_NEW(day))
      await s.call('GET', '', { token: s.viewer })
      expect(s.raw.prepare('SELECT sid FROM session_log').all()).toEqual([{ sid: 'new' }])
      vi.setSystemTime(NOW + 2 * day * 1000)
      await s.call('GET', '', { token: s.viewer })
      expect(s.raw.prepare('SELECT sid FROM session_log').all()).toEqual([])
      const bare = await setup()
      bare.raw.exec('DROP TABLE session_log; DROP TABLE session_log_batches')
      expect(await bare.call('GET', '', { token: bare.viewer })).toEqual([200, { enabled: true, until: Date.UTC(2026, 9, 18), reason: null, on: true, retainDays: 30, appEvents: {} }])
    })
  })

  describe('admin reads', () => {
    it('lists sessions (filters) and returns one session’s events in time order — admins only', async () => {
      const { call, viewer, adminT, grantIds } = await setup()
      await call('POST', '', { token: viewer, body: batch({ events: [ev(NOW - 5000, 'start', { u: '/' }), ev(NOW - 1000, 'nav', { u: '/a' })] }) })
      // A reload: the same tab's sid, a new page load; its clock interleaves.
      await call('POST', '', { token: viewer, body: batch({ pid: 'pid-222222', events: [ev(NOW - 3000, 'error', { msg: 'x' })] }) })
      await call('POST', '', { token: adminT, body: batch({ sid: 'sid-cccccc', events: [ev(NOW - 9000, 'start', { u: '/admin' })] }) })
      expect([
        await call('GET', '/sessions', { token: viewer }),
        (await call('GET', '/sessions', { token: adminT }))[1].sessions.map((r: { sid: string }) => r.sid),
        (await call('GET', '/sessions?email=ANN', { token: adminT }))[1].sessions.map((r: { sid: string }) => r.sid),
        (await call('GET', '/sessions?errors=1', { token: adminT }))[1].sessions.map((r: { sid: string }) => r.sid),
        (await call('GET', `/sessions?since=${new Date(NOW - 2000).toISOString()}`, { token: adminT }))[1].sessions.map((r: { sid: string }) => r.sid),
        await call('GET', '/sessions/nope-nope', { token: adminT }),
      ]).toEqual([
        [403, { error: 'forbidden' }],
        ['sid-aaaaaa', 'sid-cccccc'],
        ['sid-aaaaaa'],
        ['sid-aaaaaa'],
        ['sid-aaaaaa'],
        [404, { error: 'no session nope-nope' }],
      ])
      expect(await call('GET', '/sessions/sid-aaaaaa', { token: adminT })).toEqual([200, {
        session: {
          sid: 'sid-aaaaaa', email: 'ann@example.test', via: 'grant', build: 'abc1234567', ua: 'UA/1',
          started_ms: NOW - 5000, last_ms: NOW - 1000, n_events: 3, n_errors: 1, n_batches: 2, first_url: '/', received_ts: NOW / 1000, grant_id: grantIds['ann@example.test'],
        },
        events: [
          { t: NOW - 5000, k: 'start', u: '/', pid: 'pid-111111', seq: 0 },
          { t: NOW - 3000, k: 'error', msg: 'x', pid: 'pid-222222', seq: 0 },
          { t: NOW - 1000, k: 'nav', u: '/a', pid: 'pid-111111', seq: 0 },
        ],
      }])
    })
  })

  describe('latency (`/admin/latency`)', () => {
    it('summarizes logged requests per route and cache tier, per user, and the slowest; admins only', async () => {
      const { db, call, viewer, adminT } = await setup()
      const api = (t: number, u: string, ms: number, f: Record<string, unknown> = {}) => ev(t, 'api', { u, s: 200, ms, ...f })
      const post = (sid: string, token: string, events: unknown[]) => call('POST', '', { token, body: batch({ sid, events }) })
      await post('sid-aaaaaa', viewer, [
        ev(NOW - 9000, 'start', { u: '/' }),
        api(NOW - 8000, '/api/subtree?date=2026-10-10&path=b', 100, { c: 'MISS' }),
        api(NOW - 7000, '/api/subtree?date=2026-10-10T0702&path=c', 300, { c: 'MISS' }),
        api(NOW - 6000, '/api/subtree?date=2026-10-10&path=b', 20, { c: 'HIT' }),
        api(NOW - 5000, '/data/2026-10-10T0702/lifecycle.json', 7),
        api(NOW - 4000, '/api/diff?from=a&to=b', 900, { s: 503, err: 'phase2 timeout' }),
      ])
      await post('sid-bbbbbb', adminT, [api(NOW - 3000, '/api/subtree?path=', 50, { c: 'kv' })])
      expect((await call('GET', '/latency', { token: viewer }))[0]).toBe(403)
      const [st, body] = await call('GET', '/latency?days=1', { token: adminT })
      expect(st).toBe(200)
      expect({ ...body, slowest: body.slowest.map((r: { u: string; ms: number }) => [r.u, r.ms]) }).toEqual({
        since: NOW - 86_400_000, requests: 6, batches: 2, capped: false,
        routes: [
          { route: '/api/subtree', cache: 'miss', n: 2, p50: 100, p90: 300, p99: 300, max: 300, errors: 0 },
          { route: '/api/diff', cache: '-', n: 1, p50: 900, p90: 900, p99: 900, max: 900, errors: 1 },
          { route: '/api/subtree', cache: 'hit', n: 1, p50: 20, p90: 20, p99: 20, max: 20, errors: 0 },
          { route: '/api/subtree', cache: 'kv', n: 1, p50: 50, p90: 50, p99: 50, max: 50, errors: 0 },
          { route: '/data/:scan/lifecycle.json', cache: '-', n: 1, p50: 7, p90: 7, p99: 7, max: 7, errors: 0 },
        ],
        users: [
          { email: 'ann@example.test', n: 5, p50: 100, p90: 900, p99: 900, max: 900, errors: 1 },
          { email: 'ops@example.test', n: 1, p50: 50, p90: 50, p99: 50, max: 50, errors: 0 },
        ],
        slowest: [
          ['/api/diff?from=a&to=b', 900], ['/api/subtree?date=2026-10-10T0702&path=c', 300], ['/api/subtree?date=2026-10-10&path=b', 100],
          ['/api/subtree?path=', 50], ['/api/subtree?date=2026-10-10&path=b', 20], ['/data/2026-10-10T0702/lifecycle.json', 7],
        ],
      })
      const [, one] = await call('GET', '/latency?days=1&sid=sid-bbbbbb', { token: adminT })
      expect([one.requests, one.users.map((u: { email: string }) => u.email)]).toEqual([1, ['ops@example.test']])
      // Paged a batch at a time: the same summary.
      expect(await latency(db, { since: NOW - 86_400_000 }, 1)).toEqual(body)
    })
  })
})

it('records the trusted grant recipient and id, never the private memo or client-supplied identity', async () => {
  const { db, raw } = await sqliteD1()
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  const gate = gateFor(env as never)!
  const { grant, token } = await gate.mint({ email: null, scopes: ['cw'], name: 'private memo', subject: { name: 'Trusted recipient' }, createdBy: 'test', expiresAt: null, maxRedeems: null })
  raw.prepare('INSERT INTO session_log_switch VALUES (1, ?, ?, ?)').run(NOW + 86_400_000, 'ops', NOW / 1000)
  const response = await onRequest({ request: new Request(`${HOST}/api/session-log`, {
    method: 'POST', headers: { authorization: `Bearer ${token}` },
    body: JSON.stringify({ sid: 'sid-actor1', pid: 'pid-actor1', seq: 0, build: 'build1', grantId: 'forged', subjectName: 'forged', events: [{ t: NOW, k: 'start', u: '/' }] }),
  }), env } as never)
  expect(response.status).toBe(204)
  expect(raw.prepare('SELECT email, via, grant_id, subject_name FROM session_log').all()).toEqual([
    { email: null, via: 'grant', grant_id: grant.id, subject_name: 'Trusted recipient' },
  ])
})

it('keeps cancelled API requests in the session while counting only real failures as errors', async () => {
  const { db, raw } = await sqliteD1()
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', PUBLIC_READ: '1' }
  raw.prepare('INSERT INTO session_log_switch VALUES (1, ?, ?, ?)').run(NOW + 86_400_000, 'ops', NOW / 1000)
  const events = [
    { t: NOW, k: 'api', u: '/cancelled', s: 0, ms: 1, err: 'abort' },
    { t: NOW + 1, k: 'api', u: '/failed', s: 0, ms: 2, err: 'network' },
    { t: NOW + 2, k: 'api', u: '/server-error', s: 500, ms: 3 },
  ]
  const response = await onRequest({ request: new Request(`${HOST}/api/session-log`, { method: 'POST', body: JSON.stringify({ sid: 'sid-cancel', pid: 'pid-cancel', seq: 0, build: 'b', events }) }), env } as never)
  expect(response.status).toBe(204)
  expect(raw.prepare('SELECT n_events, n_errors FROM session_log').all()).toEqual([{ n_events: 3, n_errors: 2 }])
  expect((await d1SessionLogStore(db).sessionDetail('sid-cancel'))?.events).toEqual(events.map(event => ({ ...event, pid: 'pid-cancel', seq: 0 })))
})
