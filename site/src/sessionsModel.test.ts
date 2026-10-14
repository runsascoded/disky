import { describe, expect, it } from 'vitest'
import { fmtClock, fmtDuration, fmtOffset, ticks, timeline, uaLabel, viewerOf } from './sessionsModel'
import { buildRegistry } from './identityRegistry'
import type { StoredEvent } from '../functions/_lib/sessionLogShape'

const T = 1_791_817_200_000
const e = (dt: number, k: StoredEvent['k'], f: Record<string, unknown> = {}, pid = 'p1'): StoredEvent =>
  ({ t: T + dt, k, pid, seq: 0, ...f }) as StoredEvent

describe('session view model', () => {
  it('renders each kind as a line, with the URL in effect per page load and errors flagged', () => {
    const events = [
      e(0, 'start', { u: '/?d=2026-10-10', ref: 'https://discord.com/channels' }),
      e(0, 'viewport', { w: 390, h: 844, dpr: 3 }),
      e(1200, 'click', { n: 'bulk', l: 'trash' }),
      e(1300, 'click', { l: 'input', x: false }),
      e(2000, 'filter', { n: 'filter', q: 'ckpt' }),
      e(2500, 'nav', { u: '/a?d=2026-10-10&f=ckpt' }),
      e(2600, 'prefetch', { w: 'cover', p: '', d: '2026-10-10' }),
      e(2700, 'api', { u: '/api/subtree?path=a', s: 200, ms: 85, c: 'HIT', qe: 'pyrmts' }),
      e(2800, 'api', { m: 'POST', u: '/api/actions', b: { items: 2, kind: 'assign' }, s: 503, ms: 1200, err: 'phase2 timeout' }),
      e(2900, 'api', { u: '/api/down', s: 0, ms: 3, err: 'TypeError: Failed to fetch' }),
      // A second page load (a reload) starts its own URL state.
      e(3000, 'start', { u: '/b' }, 'p2'),
      e(3100, 'error', { msg: 'Uncaught TypeError: x is undefined', src: '/assets/index.js:1:2' }, 'p2'),
      e(3150, 'pick', { n: 'assign owner', v: 'ann' }),
      e(3200, 'reject', { msg: 'nope' }, 'p2'),
      e(3300, 'console', { msg: 'Warning: …' }, 'p2'),
      e(3400, 'vis', { s: 'hidden' }, 'p2'),
      e(3500, 'dropped', { n: 4 }, 'p2'),
    ]
    expect(timeline(events).map(l => [l.off, l.lane, l.error, l.url, l.text])).toEqual([
      [0, 'nav', false, '/?d=2026-10-10', 'page load /?d=2026-10-10 (from https://discord.com/channels)'],
      [0, 'page', false, '/?d=2026-10-10', '390×844 @3x'],
      [1200, 'input', false, '/?d=2026-10-10', '[bulk] trash'],
      [1300, 'input', false, '/?d=2026-10-10', 'input ☐'],
      [2000, 'input', false, '/?d=2026-10-10', 'filter = “ckpt”'],
      [2500, 'nav', false, '/a?d=2026-10-10&f=ckpt', '/a?d=2026-10-10&f=ckpt'],
      [2600, 'input', false, '/a?d=2026-10-10&f=ckpt', 'cover (root) @ 2026-10-10'],
      [2700, 'api', false, '/a?d=2026-10-10&f=ckpt', 'GET /api/subtree?path=a → 200 · 85ms · HIT · pyrmts'],
      [2800, 'error', true, '/a?d=2026-10-10&f=ckpt', 'POST /api/actions {items: 2, kind: assign} → 503 · 1200ms · phase2 timeout'],
      [2900, 'error', true, '/a?d=2026-10-10&f=ckpt', 'GET /api/down → failed · 3ms · TypeError: Failed to fetch'],
      [3000, 'nav', false, '/b', 'page load /b'],
      [3100, 'error', true, '/b', 'Uncaught TypeError: x is undefined @ /assets/index.js:1:2'],
      [3150, 'input', false, '/a?d=2026-10-10&f=ckpt', 'assign owner → ann'],
      [3200, 'error', true, '/b', 'unhandled rejection: nope'],
      [3300, 'error', true, '/b', 'console.error: Warning: …'],
      [3400, 'page', false, '/b', 'tab hidden'],
      [3500, 'page', false, '/b', '4 events dropped (cap)'],
    ])
  })

  it('places ticks by time across the width', () => {
    const lines = timeline([e(0, 'start', { u: '/' }), e(250, 'click', { l: 'x' }), e(1000, 'error', { msg: 'y' })])
    expect([ticks(lines, 400), ticks(timeline([e(0, 'start', { u: '/' })]), 400)]).toEqual([
      [{ i: 0, x: 0, lane: 'nav', error: false }, { i: 1, x: 100, lane: 'input', error: false }, { i: 2, x: 400, lane: 'error', error: true }],
      [{ i: 0, x: 0, lane: 'nav', error: false }],
    ])
  })

  it('formats wall-clock times to the ms, with the date only off the start day', () => {
    // T = 2026-10-12T15:00Z: 11:00 in New York, 00:00 the next day in Tokyo.
    expect([
      fmtClock(T, T, 'UTC'),
      fmtClock(T + 66_433, T, 'UTC'),
      fmtClock(T + 10 * 3_600_000 + 5, T, 'UTC'),
      fmtClock(T + 10 * 3_600_000 + 5, T, 'America/New_York'),
      fmtClock(T + 10 * 3_600_000 + 5, T, 'Asia/Tokyo'),
      fmtClock(T + 30 * 3_600_000, T, 'America/New_York'),
    ]).toEqual([
      '15:00:00.000',
      '15:01:06.433',
      'Oct 13 01:00:00.005',
      '21:00:00.005',
      '10:00:00.005',
      'Oct 13 17:00:00.000',
    ])
  })

  it('formats durations, offsets and user agents', () => {
    expect([
      [450, 12_000, 187_000, 3_720_000].map(fmtDuration),
      [0, 62_345].map(fmtOffset),
      [
        'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
        null,
      ].map(uaLabel),
    ]).toEqual([
      ['450ms', '12s', '3m 07s', '1h 02m'],
      ['+0:00.000', '+1:02.345'],
      ['Safari · iOS', 'Chrome · macOS', '—'],
    ])
  })

  it('maps a session to its viewer: `user_emails` row, else a registry handle, else the address; guests tagged', () => {
    const reg = buildRegistry([
      { u: 'grace-hopper', aliases: ['grace@navy.example', 'amazing-grace'], name: 'Grace' },
      { u: 'alan-turing', aliases: [] },
    ])
    const emails = { 'ada@lab.example': 'ada-lovelace' }
    const rows: { email: string | null; via: string | null }[] = [
      { email: 'Ada@Lab.example', via: 'session' },
      { email: 'grace@other.example', via: 'session' },
      { email: 'alan.turing@x.example', via: 'grant' },
      { email: 'stranger@x.example', via: 'session' },
      { email: 'visitor@x.example', via: 'grant' },
      { email: null, via: 'grant' },
      { email: null, via: 'public' },
    ]
    expect(rows.map(r => viewerOf(r, emails, reg))).toEqual([
      { kind: 'user', id: 'ada-lovelace', email: 'Ada@Lab.example', guest: false },
      { kind: 'user', id: 'grace-hopper', email: 'grace@other.example', guest: false },
      { kind: 'user', id: 'alan-turing', email: 'alan.turing@x.example', guest: true },
      { kind: 'email', email: 'stranger@x.example', guest: false },
      { kind: 'email', email: 'visitor@x.example', guest: true },
      { kind: 'guest' },
      { kind: 'anonymous' },
    ])
    // No `user_emails` map (not loaded, or a lineage without the table): the registry alone.
    expect(viewerOf(rows[0], undefined, reg)).toEqual({ kind: 'email', email: 'Ada@Lab.example', guest: false })
  })
})
