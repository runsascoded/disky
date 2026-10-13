// The session logger (specs/session-log.md): batching, the flush triggers
// (interval, size, page hide), the byte split, the per-load cap, expiry and
// the server's off answer — then `install`'s wiring over a fake page, and the
// boot shim following the admin switch (against the real endpoint).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bodySummary, FILTER_DEBOUNCE_MS, FLUSH_MS, install, Logger, type Page, redactUrl, RESIZE_DEBOUNCE_MS } from './sessionLog'
import { LIMITS, type SlogBatch } from '../functions/_lib/sessionLogShape'
import { onRequest } from '../functions/api/session-log/[[path]]'
import { gateFor } from '../functions/_lib/auth'
import { sqliteD1 } from '../functions/_lib/testD1'

const T0 = Date.UTC(2026, 9, 12, 15)
const UNTIL = T0 + 86_400_000

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(T0) })
afterEach(() => { vi.useRealTimers() })

function rig(o: { until?: number; status?: number | null } = {}) {
  const sent: { batch: SlogBatch; beacon: boolean; bytes: number }[] = []
  const logger = new Logger({
    sid: 'sid-aaaaaa', pid: 'pid-111111', build: 'abc', until: o.until ?? UNTIL,
    send: async (body, beacon) => { sent.push({ batch: JSON.parse(body), beacon, bytes: new TextEncoder().encode(body).length }); return o.status ?? 204 },
  })
  return { logger, sent }
}
const kinds = (b: SlogBatch) => b.events.map(e => e.k)

describe('Logger', () => {
  it('batches until the 10 s flush; each flush is the next seq', async () => {
    const { logger, sent } = rig()
    logger.begin()
    logger.log('vis', { s: 'visible' })
    vi.advanceTimersByTime(1000)
    logger.log('click', { n: 'scan', l: 'scan' })
    expect(sent).toEqual([])
    vi.advanceTimersByTime(FLUSH_MS - 1000)
    logger.log('nav', { u: '/a' })
    vi.advanceTimersByTime(FLUSH_MS)
    vi.advanceTimersByTime(FLUSH_MS)  // nothing buffered: no post
    expect(sent).toEqual([
      { beacon: false, bytes: sent[0].bytes, batch: { sid: 'sid-aaaaaa', pid: 'pid-111111', seq: 0, build: 'abc', events: [
        { t: T0, k: 'vis', s: 'visible' },
        { t: T0 + 1000, k: 'click', n: 'scan', l: 'scan' },
      ] } },
      { beacon: false, bytes: sent[1].bytes, batch: { sid: 'sid-aaaaaa', pid: 'pid-111111', seq: 1, build: 'abc', events: [
        { t: T0 + FLUSH_MS, k: 'nav', u: '/a' },
      ] } },
    ])
  })

  it(`flushes at ${LIMITS.flushEvents} buffered events without waiting`, () => {
    const { logger, sent } = rig()
    for (let i = 0; i < LIMITS.flushEvents + 1; i++) logger.log('vis', { s: 'visible' })
    expect([sent.map(s => s.batch.events.length), logger.buffered.length]).toEqual([[LIMITS.flushEvents], 1])
  })

  it(`splits a flush so every body stays under ${LIMITS.batchBytes} bytes, in order`, () => {
    const { logger, sent } = rig()
    for (let i = 0; i < 150; i++) logger.log('error', { msg: `${i}:${'x'.repeat(1000)}` })
    logger.flush(true)
    const ns = sent.map(s => s.batch.events.length)
    expect([
      sent.map(s => s.batch.seq),
      sent.every(s => s.bytes <= LIMITS.batchBytes && s.beacon),
      ns.reduce((a, b) => a + b),
      sent.flatMap(s => s.batch.events.map(e => Number(String(e.msg).split(':')[0]))),
    ]).toEqual([[0, 1, 2], true, 150, Array.from({ length: 150 }, (_, i) => i)])
  })

  it('cuts strings past the stack limit', () => {
    const { logger } = rig()
    logger.log('error', { msg: 'y'.repeat(2000) })
    expect(String(logger.buffered[0].msg)).toEqual(`${'y'.repeat(1499)}…`)
  })

  it(`past ${LIMITS.sessionEvents} events a page load counts what it drops instead`, () => {
    const { logger, sent } = rig()
    for (let i = 0; i < LIMITS.sessionEvents + 3; i++) logger.log('vis', { s: 'visible' })
    logger.flush(false)
    const last = sent.at(-1)!.batch.events
    expect([sent.reduce((n, s) => n + s.batch.events.length, 0), last[0]]).toEqual([LIMITS.sessionEvents + 1, { t: T0, k: 'dropped', n: 3 }])
  })

  it('expires: what was buffered goes out, later events are dropped, the wiring is undone', () => {
    const { logger, sent } = rig({ until: T0 + 5000 })
    const undo = vi.fn()
    logger.onStop = undo
    logger.begin()
    logger.log('vis', { s: 'visible' })
    vi.setSystemTime(T0 + 5000)
    logger.log('click', { l: 'late' })
    logger.log('click', { l: 'later' })
    vi.advanceTimersByTime(FLUSH_MS * 3)
    expect([sent.map(s => kinds(s.batch)), logger.stopped, undo.mock.calls.length]).toEqual([[['vis']], true, 1])
  })

  it('a 503 (off server-side) stops it', async () => {
    const { logger, sent } = rig({ status: 503 })
    logger.log('vis', { s: 'visible' })
    logger.flush(false)
    await vi.runAllTimersAsync()
    logger.log('vis', { s: 'hidden' })
    logger.flush(false)
    expect([sent.length, logger.stopped]).toEqual([1, true])
  })
})

describe('redactUrl / bodySummary', () => {
  const O = 'https://site.test'
  it('masks secret params, keeps the rest; cross-origin keeps origin + path', () => {
    expect([
      redactUrl('https://site.test/x/y?d=2026-10-10&key=abc&f=foo#h', O),
      redactUrl('/auth/email/verify?code=123456&next=%2F', O),
      redactUrl('https://storage.googleapis.com/b/o?X-Goog-Signature=zz', O),
      redactUrl('/?TOKEN=1', O),
    ]).toEqual([
      '/x/y?d=2026-10-10&key=…&f=foo#h',
      '/auth/email/verify?code=…&next=%2F',
      'https://storage.googleapis.com/b/o',
      '/?TOKEN=…',
    ])
  })
  it('summarizes a request body as counts and kinds', () => {
    expect([
      bodySummary(JSON.stringify({ items: [1, 2, 3], kind: 'assign', owner: 'ann', memo: 'secret plans', n: 4, x: null })),
      bodySummary('not json'),
      bodySummary(JSON.stringify([1, 2])),
      bodySummary(undefined),
      bodySummary(new URLSearchParams('a=1')),
    ]).toEqual([
      { items: 3, kind: 'assign', owner: 'string', memo: 'string', n: 'number', x: 'null' },
      { type: 'text', len: 8 },
      { type: 'array', len: 2 },
      null,
      { type: 'URLSearchParams' },
    ])
  })
})

// A minimal element: tag, attributes, text, parent; `closest` over the selectors `install` uses.
interface FakeEl { tagName: string; textContent: string; type?: string; checked?: boolean; value?: string; id?: string; getAttribute(k: string): string | null; closest(sel: string): FakeEl | null }
function el(tag: string, attrs: Record<string, string> = {}, text = '', parent: FakeEl | null = null, extra: Partial<FakeEl> = {}): FakeEl {
  const self: FakeEl = {
    tagName: tag.toUpperCase(), textContent: text, ...extra,
    getAttribute: k => attrs[k] ?? null,
    closest(sel) {
      const m = sel === '[data-log]' ? 'data-log' in attrs
        : sel === '[data-log-input]' ? 'data-log-input' in attrs
        : ['BUTTON', 'A'].includes(self.tagName) || 'data-log' in attrs || 'role' in attrs || self.type === 'checkbox'
      return m ? self : parent?.closest(sel) ?? null
    },
  }
  return self
}
const fire = (t: EventTarget, type: string, target?: unknown, props: Record<string, unknown> = {}) => {
  const e = new Event(type)
  if (target) Object.defineProperty(e, 'target', { value: target })
  for (const [k, v] of Object.entries(props)) Object.defineProperty(e, k, { value: v })
  t.dispatchEvent(e)
}

function page() {
  const origin = 'https://site.test'
  const loc = { href: `${origin}/?d=2026-10-10&key=SEKRIT`, origin }
  const go = (_s: unknown, _t: unknown, u: unknown) => { loc.href = new URL(String(u), loc.href).href }
  const responses: Record<string, () => Promise<Response>> = {
    '/api/subtree': async () => new Response('{}', { status: 200, headers: { 'x-cache': 'HIT', 'x-query-engine': 'pyrmts' } }),
    '/api/diff': async () => new Response(JSON.stringify({ error: 'phase2 timeout' }), { status: 503 }),
    '/api/actions': async () => new Response('{}', { status: 200 }),
    '/api/down': async () => { throw new TypeError('Failed to fetch') },
    '/api/session-log': async () => new Response(null, { status: 204 }),
  }
  const origFetch = vi.fn(async (u: string) => responses[u.split('?')[0]]())
  const win = Object.assign(new EventTarget(), {
    location: loc,
    history: { pushState: go, replaceState: go },
    innerWidth: 1280, innerHeight: 800, devicePixelRatio: 2,
    fetch: origFetch as unknown as typeof fetch,
  })
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible', referrer: '' })
  const origError = vi.fn()
  const con = { error: origError }
  let clock = 0
  const p: Page = { win, doc, con, perfNow: () => clock }
  return { p, win, doc, con, origFetch, origError, tick: (ms: number) => { clock += ms } }
}

describe('install', () => {
  it('records nav, clicks, the filter (debounced), picks, fetches, errors and viewport/visibility; a hidden page beacons', async () => {
    const { logger, sent } = rig()
    const { p, win, doc, con, origError, tick } = page()
    const undo = install(p, logger)
    // URL state.
    win.history.pushState(null, '', '/a/b?d=2026-10-10&f=ckpt')
    win.history.replaceState(null, '', '/a/b?d=2026-10-10&f=ckpt')  // unchanged: not logged
    // Clicks: a named control, a nested icon in a plain button, a checkbox; a click on nothing interactive.
    const bar = el('div', { 'data-log': 'bulk' })
    fire(doc, 'click', el('button', { 'aria-label': 'trash' }, '', bar))
    fire(doc, 'click', el('svg', {}, '', el('button', {}, '  Share…  ')))
    fire(doc, 'click', el('input', {}, '', null, { type: 'checkbox', checked: true }))
    fire(doc, 'click', el('div', {}, 'just text'))
    // The filter: three keystrokes inside the debounce = one event; an unlogged input = none.
    const box = el('input', { 'data-log-input': 'filter' })
    for (const v of ['c', 'ck', 'ckpt']) { fire(doc, 'input', Object.assign(box, { value: v })); vi.advanceTimersByTime(100) }
    fire(doc, 'input', Object.assign(el('input', { 'aria-label': 'deletion note' }), { value: 'private memo' }))
    vi.advanceTimersByTime(FILTER_DEBOUNCE_MS)
    // A <select>.
    fire(doc, 'change', Object.assign(el('select', { 'aria-label': 'assign owner' }), { value: 'ann' }))
    // Fetches: a hit, a 503 with an error code, a POST (body summarized), a network failure, the log's own endpoint.
    await win.fetch('/api/subtree?d=2026-10-10&key=SEKRIT')
    tick(40)
    await win.fetch('/api/diff?a=1')
    await win.fetch('/api/actions', { method: 'POST', body: JSON.stringify({ items: ['a', 'b'], kind: 'assign', memo: 'x' }) })
    await win.fetch('/api/down').catch(() => {})
    await win.fetch('/api/session-log', { method: 'POST', body: '{}' })
    await vi.runOnlyPendingTimersAsync()
    // Errors.
    const err = new Error('kaboom')
    err.stack = 'Error: kaboom\n    at f (https://site.test/assets/index.js:1:2)'
    fire(win, 'error', undefined, { message: 'Uncaught Error: kaboom', filename: 'https://site.test/assets/index.js?key=x', lineno: 1, colno: 2, error: err })
    fire(win, 'unhandledrejection', undefined, { reason: 'nope' })
    con.error('Warning:', new Error('bad prop'))
    // Viewport (debounced) and visibility.
    Object.assign(win, { innerWidth: 390, innerHeight: 844 })
    fire(win, 'resize'); fire(win, 'resize')
    vi.advanceTimersByTime(RESIZE_DEBOUNCE_MS)
    ;(doc as { visibilityState: string }).visibilityState = 'hidden'
    fire(doc, 'visibilitychange')
    expect(sent.map(s => [s.beacon, s.batch.seq])).toEqual([[true, 0]])
    const evs = sent[0].batch.events.map(({ t, ...e }) => ({ dt: t - T0, ...e }))
    expect(evs).toEqual([
      { dt: 0, k: 'start', u: '/?d=2026-10-10&key=…' },
      { dt: 0, k: 'viewport', w: 1280, h: 800, dpr: 2 },
      { dt: 0, k: 'nav', u: '/a/b?d=2026-10-10&f=ckpt' },
      { dt: 0, k: 'click', n: 'bulk', l: 'trash' },
      { dt: 0, k: 'click', l: 'Share…' },
      { dt: 0, k: 'click', l: 'input', x: true },
      { dt: 1000, k: 'filter', n: 'filter', q: 'ckpt' },
      { dt: 1100, k: 'pick', n: 'assign owner', v: 'ann' },
      { dt: 1100, k: 'api', u: '/api/subtree?d=2026-10-10&key=…', s: 200, ms: 0, c: 'HIT', qe: 'pyrmts' },
      { dt: 1100, k: 'api', m: 'POST', u: '/api/actions', b: { items: 2, kind: 'assign', memo: 'string' }, s: 200, ms: 0 },
      { dt: 1100, k: 'api', u: '/api/diff?a=1', s: 503, ms: 0, err: 'phase2 timeout' },
      { dt: 1100, k: 'api', u: '/api/down', s: 0, ms: 0, err: 'TypeError: Failed to fetch' },
      { dt: 1100, k: 'error', msg: 'Uncaught Error: kaboom', src: '/assets/index.js?key=…:1:2', st: err.stack },
      { dt: 1100, k: 'reject', msg: 'nope' },
      { dt: 1100, k: 'console', msg: 'Warning: Error: bad prop' },
      { dt: 1600, k: 'viewport', w: 390, h: 844, dpr: 2 },
      { dt: 1600, k: 'vis', s: 'hidden' },
    ])
    expect(origError.mock.calls.length).toBe(1)
    // Undo restores every patch.
    undo()
    expect([win.fetch === p.win.fetch, con.error === origError]).toEqual([true, true])
    win.history.pushState(null, '', '/c')
    fire(win, 'pagehide')
    expect(sent.length).toBe(1)
  })

  it('a pending filter edit is committed by the page-hide flush', () => {
    const { logger, sent } = rig()
    const { p, win, doc } = page()
    install(p, logger)
    fire(doc, 'input', Object.assign(el('input', { 'data-log-input': 'filter' }), { value: 'tmp' }))
    fire(win, 'pagehide')
    expect(sent.map(s => [s.beacon, kinds(s.batch)])).toEqual([[true, ['start', 'viewport', 'filter']]])
  })

  it('per event: well under 20 µs (measured)', () => {
    vi.useRealTimers()
    const { logger } = rig()
    const N = 20_000
    const t = performance.now()
    for (let i = 0; i < N; i++) logger.log('click', { n: 'scan', l: 'scan picker' })
    const us = (performance.now() - t) * 1000 / N
    expect(us < 20).toBe(true)
  })
})

describe('boot shim', () => {
  /** A fresh page load: `cfg` is the config the server answers, or the page's `fetch`. */
  const boot = async (cfg: unknown) => {
    vi.resetModules()
    const start = vi.fn(() => ({ log: vi.fn(), logger: { onStop: null } }))
    vi.doMock('./sessionLog', () => ({ start }))
    vi.stubGlobal('addEventListener', () => {})
    vi.stubGlobal('removeEventListener', () => {})
    vi.stubGlobal('fetch', typeof cfg === 'function' ? cfg : vi.fn(async () => new Response(JSON.stringify(cfg), { status: 200 })))
    const m = await import('./sessionLogBoot')
    m.slog('prefetch', { w: 'cover', p: 'a' })
    await m.bootSessionLog()
    return start.mock.calls
  }
  afterEach(() => { vi.unstubAllGlobals(); vi.doUnmock('./sessionLog') })

  it('follows the admin switch at runtime: off → on (through the chosen day) → off → on, then expired', async () => {
    const { db } = await sqliteD1('cw')
    const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
    const gate = gateFor(env as never)!
    const tok = async (email: string, scopes: string[]) =>
      (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
    const viewer = await tok('ann@example.test', ['cw'])
    const adminT = await tok('ops@example.test', ['cw', 'admin'])
    const api = (path: string, token: string, init: RequestInit = {}) => onRequest({
      request: new Request(`https://site.example.test${path}`, { ...init, headers: { authorization: `Bearer ${token}` } }),
      env,
      params: path === '/api/session-log' ? {} : { path: path.replace('/api/session-log/', '').split('/') },
    } as never)
    const put = (body: unknown) => api('/api/session-log/switch', adminT, { method: 'PUT', body: JSON.stringify(body) })
    // The page's own fetch, signed in as the viewer.
    const page = vi.fn(async (input: RequestInfo | URL) => api(String(input), viewer))
    const seen = [await boot(page)]
    await put({ enabled: true, until: '2026-10-14' })
    seen.push(await boot(page))
    await put({ enabled: false })
    seen.push(await boot(page))
    await put({ enabled: true, until: '2026-10-12' })
    seen.push(await boot(page))
    vi.setSystemTime(Date.UTC(2026, 9, 13))
    seen.push(await boot(page))
    const on = (until: number) => [[{ until, early: [{ t: T0, k: 'prefetch', w: 'cover', p: 'a' }], build: '' }]]
    expect(seen).toEqual([[], on(Date.UTC(2026, 9, 15)), [], on(Date.UTC(2026, 9, 13)), []])
  })

  it('off, expired, or enabled: false (even with an end) → no logger; on → the logger gets the held events', async () => {
    expect([
      await boot({ enabled: false, until: null, reason: 'off' }),
      await boot({ enabled: false, until: UNTIL, reason: 'no viewer identity' }),
      await boot({ enabled: true, until: T0 - 1 }),
      await boot({ enabled: true, until: UNTIL }),
    ]).toEqual([
      [],
      [],
      [],
      [[{ until: UNTIL, early: [{ t: T0, k: 'prefetch', w: 'cover', p: 'a' }], build: '' }]],
    ])
  })
})
