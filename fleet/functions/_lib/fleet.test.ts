/** The fleet document over stubbed deployments: what each is asked (with its own token), and what the document says,
 *  whole, including each way a deployment can fail to answer. */
import { describe, expect, it } from 'vitest'
import type { Env } from './env'
import { type FleetDoc, fleetMeta, parseFleet, readFleet, runsOf } from './fleet'
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

const runWire = (run_id: string, scan: string, kind: string, status: string, started: number, secs: number, cost: number | null, failed_at: string | null, issues: object[]) =>
  ({ run: { run_id, scan, kind, status, started_ts: started, store: 'x', parent: null }, secs, cost_usd: cost, failed_at, phase_issues: issues, spans: [], downstream: 0 })

const ANSWERS = {
  'https://a.test/api/health': { now: NOW, freshness: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, stores: [] }, gaps: { path: 1, search: 2 }, scans: [] },
  'https://a.test/api/scan-runs': {
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
      ]),
      runWire('r2', '2026-11-06', 'scan', 'failed', NOW - 31 * H, 3600, null, 'index', []),
    ],
  },
  'https://a.test/api/series?store=meta': {
    path: '',
    points: [
      { date: '2026-10-01', b: 1, o: 1 },
      { date: '2026-10-31', b: 100, o: 1 },
      { date: '2026-11-06', b: 200, o: 1 },
      { date: '2026-11-06T1200', b: 250, o: 1 },
      { date: '2026-11-07', b: 300, o: 1 },
      { date: 'bogus', b: 9, o: 1 },
    ],
  },
  'https://c.test/api/health': new Status(500),
  'https://c.test/api/scan-runs': { configured: false, runs: [] },
  'https://c.test/api/series?store=meta': null,
}

describe('/api/fleet\'s document over stubbed deployments', () => {
  it('asks each deployment for its health, scan runs and /meta series with its own token; one without a token is not asked', async () => {
    const seen: Seen[] = []
    await readFleet(env(), NOW, stubFetch(ANSWERS, seen))
    expect(seen).toEqual([
      { url: 'https://a.test/api/health', authorization: 'Bearer tok-a' },
      { url: 'https://a.test/api/scan-runs', authorization: 'Bearer tok-a' },
      { url: 'https://a.test/api/series?store=meta', authorization: 'Bearer tok-a' },
      { url: 'https://c.test/api/health', authorization: 'Bearer tok-c' },
      { url: 'https://c.test/api/scan-runs', authorization: 'Bearer tok-c' },
      { url: 'https://c.test/api/series?store=meta', authorization: 'Bearer tok-c' },
    ])
  })

  it('folds the answers: maintenance and no-op runs skipped, open issues only, /meta now / 1 d / 7 d and the newest scan per day; each failure named', async () => {
    const r1 = { run_id: 'r1', scan: '2026-11-07', status: 'succeeded', started: NOW - 7 * H, secs: 7668, cost_usd: 0.5, failed_at: null, open_issues: ['dups'] }
    const r2 = { run_id: 'r2', scan: '2026-11-06', status: 'failed', started: NOW - 31 * H, secs: 3600, cost_usd: null, failed_at: 'index', open_issues: [] }
    const noToken = 'no token (FLEET_TOKEN_B unset)'
    const expected: FleetDoc = {
      now: NOW,
      deployments: [
        {
          name: 'a', url: 'https://a.test', color: '#111111',
          health: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, gaps: 3 },
          last: r1,
          runs: [r1, r2],
          meta: {
            now: 300, d1: 250, d7: 100,
            daily: [
              { scan: '2026-10-31', t: t('2026-10-31T00:00Z'), bytes: 100 },
              { scan: '2026-11-06T1200', t: t('2026-11-06T12:00Z'), bytes: 250 },
              { scan: '2026-11-07', t: t('2026-11-07T00:00Z'), bytes: 300 },
            ],
          },
          errors: {},
        },
        { name: 'b', url: 'https://b.test', color: '#222222', health: null, last: null, runs: [], meta: null, errors: { health: noToken, runs: noToken, meta: noToken } },
        { name: 'c', url: 'https://c.test', color: '#333333', health: null, last: null, runs: [], meta: null, errors: { health: 'HTTP 500', runs: 'scan runs are not set up', meta: 'HTTP 403' } },
      ],
    }
    expect(await readFleet(env(), NOW, stubFetch(ANSWERS))).toEqual(expected)
  })

  it('keeps the newest `FLEET_RUNS` runs', async () => {
    const doc = await readFleet(env({ FLEET_RUNS: '1' }), NOW, stubFetch(ANSWERS))
    expect(doc.deployments[0].runs.map(r => r.run_id)).toEqual(['r1'])
    expect([runsOf(env()), runsOf(env({ FLEET_RUNS: '0' })), runsOf(env({ FLEET_RUNS: 'x' })), runsOf(env({ FLEET_RUNS: '30' }))]).toEqual([14, 14, 14, 30])
  })

  it('a meta store with no points has no sizes', () => {
    expect(fleetMeta([], NOW)).toEqual({ now: null, d1: null, d7: null, daily: [] })
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
