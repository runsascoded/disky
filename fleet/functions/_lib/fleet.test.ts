/** The fleet document over stubbed deployments: what each is asked (with its own token), and what the document says,
 *  whole, including each way a deployment can fail to answer. */
import { describe, expect, it } from 'vitest'
import type { Env } from './env'
import { ERROR_MAX, type FleetDoc, fleetMeta, oneLine, parseFleet, readFleet, runsOf } from './fleet'
import { NOW, type Seen, Status, stubFetch } from './testFleet'

const t = (iso: string) => Date.parse(iso) / 1000
const H = 3600

const env = (extra: Partial<Env> = {}): Env => ({
  ASSETS: { fetch: async () => new Response(null, { status: 404 }) },
  FLEET_JSON: JSON.stringify([
    { name: 'a', url: 'https://a.test', token: 'FLEET_TOKEN_A', color: '#111111' },
    { name: 'b', url: 'https://b.test', token: 'FLEET_TOKEN_B', color: '#222222' },
    { name: 'c', url: 'https://c.test/', token: 'FLEET_TOKEN_C', color: '#333333' },
  ]),
  FLEET_TOKEN_A: 'tok-a',
  FLEET_TOKEN_C: 'tok-c',
  ...extra,
})

const runWire = (run_id: string, scan: string, kind: string, status: string, started: number, secs: number, cost: number | null, failed_at: string | null, issues: object[], trigger?: string, run: object = {}, spans: object[] = []) =>
  ({ run: { run_id, scan, kind, status, started_ts: started, store: 'x', parent: null, ...(trigger ? { trigger } : {}), ...run }, secs, cost_usd: cost, failed_at, phase_issues: issues, spans, downstream: 0 })
const span = (phase: string, start: number, end: number, status = 'done', note: string | null = null) => ({ phase, start, end, status, note, lane: 0 })
const THREAD = 'https://slack.test/archives/C0ADMIN/p1762498800000100'

const ANSWERS = {
  'https://a.test/api/health': { now: NOW, freshness: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, stores: [] }, gaps: { path: 1, search: 2 }, scans: [] },
  'https://a.test/api/scan-runs?emoji=1': {
    configured: true,
    project: null,
    runs: [
      runWire('m1', '2026-11-07', 'compact', 'succeeded', NOW - H, 600, 0.02, null, []),
      runWire('n1', '2026-11-07', 'scan', 'nop', NOW - 2 * H, 0, 0, null, []),
      runWire('i1', '2026-11-07', 'access-ingest', 'succeeded', NOW - 3 * H, 30, 0, null, []),
      runWire('p1', '2026-11-07', 'reproc', 'succeeded', NOW - 4 * H, 900, 0.03, null, []),
      runWire('q1', '2026-11-07', 'phase-recovery', 'succeeded', NOW - 5 * H, 120, 0.01, null, []),
      runWire('r1', '2026-11-07', 'scan', 'succeeded', NOW - 7 * H, 7668, 0.5, null, [
        { phase: 'dups', outcome: 'failed', recovery: null, passing: null },
        { phase: 'probe', outcome: 'failed', recovery: { run_id: 'r0' }, passing: null },
        { phase: 'check', outcome: 'failed', recovery: null, passing: { run_id: 'r9' } },
      ], 'manual', { thread_url: THREAD, trigger_emoji: ':ryan:' }, [
        span('queue', 0, 300), span('list b1', 300, 4000), span('list b2', 350, 5000), span('dups', 5000, 7600, 'done', 'failed (exit 1)'),
      ]),
      runWire('r2', '2026-11-06', 'scan', 'failed', NOW - 31 * H, 3600, null, 'index', [], undefined, { error: 'boom <x>\n  `y`' }, [span('index', 100, 3600, 'failed')]),
      runWire('r3', '2026-10-18', 'scan', 'succeeded', NOW - 20 * 24 * H, 3000, 0.4, null, [], 'schedule', { thread_url: null, trigger_emoji: ':ryan:' }),
      runWire('r4', '2026-10-08', 'scan', 'succeeded', NOW - 30 * 24 * H - H, 3000, 0.4, null, [], 'schedule'),
    ],
  },
  'https://a.test/api/series?store=meta': {
    path: '',
    points: [
      { date: '2026-10-01', bytes: 1, objects: 1 },
      { date: '2026-10-31', bytes: 100, objects: 1 },
      { date: '2026-11-06', bytes: 200, objects: 1 },
      { date: '2026-11-06T1200', bytes: 250, objects: 1 },
      { date: '2026-11-07', bytes: 300, objects: 1 },
      { date: 'bogus', bytes: 9, objects: 1 },
    ],
  },
  'https://a.test/api/series': {
    path: '',
    points: [
      { date: '2026-10-01', bytes: 1, objects: 5 },
      { date: '2026-11-06', bytes: 1, objects: 10 },
      { date: '2026-11-06T1200', bytes: 1, objects: 12 },
      { date: '2026-11-07', bytes: 1, objects: 15 },
      { date: 'bogus', bytes: 1, objects: 99 },
    ],
  },
  'https://c.test/api/health': new Status(500),
  'https://c.test/api/scan-runs?emoji=1': { configured: false, runs: [] },
  'https://c.test/api/series?store=meta': null,
  'https://c.test/api/series': null,
}

describe('/api/fleet\'s document over stubbed deployments', () => {
  it('asks each deployment for its health, scan runs and /meta series with its own token; one without a token is not asked', async () => {
    const seen: Seen[] = []
    await readFleet(env(), NOW, stubFetch(ANSWERS, seen))
    expect(seen).toEqual([
      { url: 'https://a.test/api/health', authorization: 'Bearer tok-a' },
      { url: 'https://a.test/api/scan-runs?emoji=1', authorization: 'Bearer tok-a' },
      { url: 'https://a.test/api/series?store=meta', authorization: 'Bearer tok-a' },
      { url: 'https://a.test/api/series', authorization: 'Bearer tok-a' },
      { url: 'https://c.test/api/health', authorization: 'Bearer tok-c' },
      { url: 'https://c.test/api/scan-runs?emoji=1', authorization: 'Bearer tok-c' },
      { url: 'https://c.test/api/series?store=meta', authorization: 'Bearer tok-c' },
      { url: 'https://c.test/api/series', authorization: 'Bearer tok-c' },
    ])
  })

  it('folds the answers: maintenance and no-op runs skipped, runs over the last 30 days, each day\'s scan cost since last month began, open issues only, each run\'s thread as its site says it (none from a site that predates it), a failed run\'s error on one line, each run\'s seconds per step family (from its phases, when listed), /meta now / 1 d / 7 d and the newest scan per day; each failure named', async () => {
    // r1's clock: queue 0–300, list 300–5000 (two buckets, one family), dups 5000–7600 (failed), no step 7600–7668
    const r1 = {
      run_id: 'r1', scan: '2026-11-07', status: 'succeeded', trigger: 'manual' as const, started: NOW - 7 * H, secs: 7668, cost_usd: 0.5, failed_at: null, open_issues: ['dups'], thread_url: THREAD, trigger_emoji: ':ryan:',
      families: [['queue', 300], ['list', 4700], ['dups', 2600], ['', 68]] as [string, number][], bad: [['dups', 'failed']] as [string, 'failed'][],
    }
    const r2 = {
      run_id: 'r2', scan: '2026-11-06', status: 'failed', trigger: null, started: NOW - 31 * H, secs: 3600, cost_usd: null, failed_at: 'index', open_issues: [], error: "boom ‹x› 'y'",
      families: [['', 100], ['index', 3500]] as [string, number][], bad: [['index', 'failed']] as [string, 'failed'][],
    }
    const r3 = { run_id: 'r3', scan: '2026-10-18', status: 'succeeded', trigger: 'schedule' as const, started: NOW - 20 * 24 * H, secs: 3000, cost_usd: 0.4, failed_at: null, open_issues: [], thread_url: null }
    const noToken = 'no token (FLEET_TOKEN_B unset)'
    const expected: FleetDoc = {
      now: NOW,
      days: 30,
      deployments: [
        {
          name: 'a', url: 'https://a.test', color: '#111111',
          health: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, gaps: 3 },
          last: r1,
          runs: [r1, r2, r3],
          meta: {
            now: 300, d1: 250, d7: 100,
            daily: [
              { scan: '2026-10-31', t: t('2026-10-31T00:00Z'), bytes: 100 },
              { scan: '2026-11-06T1200', t: t('2026-11-06T12:00Z'), bytes: 250 },
              { scan: '2026-11-07', t: t('2026-11-07T00:00Z'), bytes: 300 },
            ],
          },
          objects: [
            { scan: '2026-11-06T1200', t: t('2026-11-06T12:00Z'), objects: 12, bytes: 1 },
            { scan: '2026-11-07', t: t('2026-11-07T00:00Z'), objects: 15, bytes: 1 },
          ],
          // from Oct 1 (the month before NOW's), reaching past `runs`' 30 days (r4); unpriced (r2) and no-op runs left out
          cost_daily: [
            { t: t('2026-10-08T00:00Z'), usd: 0.4 },
            { t: t('2026-10-18T00:00Z'), usd: 0.4 },
            { t: t('2026-11-07T00:00Z'), usd: 0.5 },
          ],
          errors: {},
        },
        { name: 'b', url: 'https://b.test', color: '#222222', health: null, last: null, runs: [], meta: null, objects: null, errors: { health: noToken, runs: noToken, meta: noToken, objects: noToken } },
        { name: 'c', url: 'https://c.test', color: '#333333', health: null, last: null, runs: [], meta: null, objects: null, errors: { health: 'HTTP 500', runs: 'scan runs are not set up', meta: 'HTTP 403', objects: 'HTTP 403' } },
      ],
    }
    expect(await readFleet(env(), NOW, stubFetch(ANSWERS))).toEqual(expected)
  })

  it('keeps the newest `FLEET_RUNS` runs', async () => {
    const doc = await readFleet(env({ FLEET_RUNS: '1' }), NOW, stubFetch(ANSWERS))
    expect(doc.deployments[0].runs.map(r => r.run_id)).toEqual(['r1'])
    expect([runsOf(env()), runsOf(env({ FLEET_RUNS: '0' })), runsOf(env({ FLEET_RUNS: 'x' })), runsOf(env({ FLEET_RUNS: '30' }))]).toEqual([200, 200, 200, 30])
  })

  it('splits only the newest 30 runs by step family (what the card draws)', async () => {
    const many = Array.from({ length: 32 }, (_, i) => runWire(`s${i}`, '2026-11-07', 'scan', 'succeeded', NOW - (i + 1) * H, 100, null, null, [], 'schedule', {}, [span('list b', 0, 100)]))
    const doc = await readFleet(env(), NOW, stubFetch({ ...ANSWERS, 'https://a.test/api/scan-runs?emoji=1': { configured: true, runs: many } }))
    expect(doc.deployments[0].runs.map(r => [r.run_id, r.families ?? null])).toEqual(
      many.map((_, i) => [`s${i}`, i < 30 ? [['list', 100]] : null]),
    )
  })

  it('an error on one line: whitespace collapsed, Slack markup made inert, clipped', () => {
    const long = 'x'.repeat(ERROR_MAX + 5)
    expect([oneLine(' a\n\tb  <c> `d` & e '), oneLine(long)]).toEqual(["a b ‹c› 'd' & e", `${'x'.repeat(ERROR_MAX - 1)}…`])
  })

  it('a meta store with no points has no sizes', () => {
    expect(fleetMeta([], NOW)).toEqual({ now: null, d1: null, d7: null, daily: [] })
  })

  it('a deployment whose site predates the descriptive names (`b`) reads as one that serves `bytes`', () => {
    const day = '2026-11-07'
    const [old, now] = [fleetMeta([{ date: day, b: 300 }], NOW), fleetMeta([{ date: day, bytes: 300 }], NOW)]
    expect([old.now, old]).toEqual([300, now])
  })
})

describe('FLEET_JSON', () => {
  it('parses a list of deployments, trimming a URL\'s trailing slash', () => {
    expect(parseFleet('[{"name":"gcs","url":"https://gcs.oa.dev/","token":"FLEET_TOKEN_GCS","color":"#EA4335"}]'))
      .toEqual([{ name: 'gcs', url: 'https://gcs.oa.dev', token: 'FLEET_TOKEN_GCS', color: '#EA4335' }])
    expect(parseFleet(undefined)).toEqual([])
  })
  it('refuses anything else, saying what', () => {
    const errors = ['{}', '[{"name":"a","url":"u","token":"T"}]', '[{"name":"a","url":"u","token":"T","color":"c"},{"name":"a","url":"v","token":"U","color":"d"}]'].map(raw => {
      try { parseFleet(raw); return null } catch (e) { return (e as Error).message }
    })
    expect(errors).toEqual(['FLEET_JSON: not a list', 'FLEET_JSON[0]: no color', 'FLEET_JSON: duplicate names'])
  })
})
