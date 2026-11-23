/** The deployment status card as the site draws it (`deployData.ts`, open question #87): its input from the store's D1
 *  rows and series, the version its URL names, the signed URL, and `GET /api/scan-runs/card-url`. */
import { dailyPoints } from '@disk-tree/pages-kit/og/deployCard'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onRequestGet as cardUrlRoute } from '../../api/scan-runs/card-url'
import { sqliteD1 } from '../testD1'
import { colorParam, DEFAULT_COLOR, deployCardUrl, deployData, deployName, deployParams, deployVersion, type DeployData, type DeployEnv } from './deployData'
import { ogKey, resolveImage } from './sign'

// The card renderer's wasm (`serve.ts`, which the route imports for the deployment's key): nothing here draws.
vi.mock('./vendor/resvg.wasm', () => ({ default: new Uint8Array() }))

const DAY = 86400
/** 2026-11-22 12:00 UTC. */
const NOW = Date.UTC(2026, 10, 22, 12) / 1000
const ORIGIN = 'http://localhost'

/** A primary store's runs: two scans the card draws (the newer manual, its `digest` step failed), and what it leaves out
 *  (a run 40 days old, a no-op, a listing job's own run, another store's). */
const SEED = `
INSERT INTO scan_runs (run_id, store, scan, kind, parent, status, started_ts, finished_ts, cost_usd, source, updated_ts, trigger) VALUES
  ('old', 'primary', '2026-10-13T0700', 'scan', NULL, 'succeeded', ${NOW - 40 * DAY}, ${NOW - 40 * DAY + 500}, 1.0, 'job', 0, 'schedule'),
  ('r1', 'primary', '2026-11-20T1200', 'scan', NULL, 'succeeded', ${NOW - 2 * DAY}, ${NOW - 2 * DAY + 600}, 1.25, 'job', 0, 'schedule'),
  ('r2', 'primary', '2026-11-21T1200', 'scan', NULL, 'succeeded', ${NOW - DAY}, ${NOW - DAY + 400}, NULL, 'job', 0, 'manual'),
  ('nop', 'primary', '2026-11-21T1800', 'scan', NULL, 'nop', ${NOW - DAY / 2}, ${NOW - DAY / 2 + 5}, NULL, 'job', 0, 'schedule'),
  ('lst', 'primary', '2026-11-21T2000', 'listing', NULL, 'succeeded', ${NOW - DAY / 3}, ${NOW - DAY / 3 + 50}, 0.1, 'job', 0, NULL),
  ('m1', 'meta', '2026-11-21T1200', 'scan', NULL, 'succeeded', ${NOW - DAY}, ${NOW - DAY + 90}, 0.01, 'job', 0, 'schedule');
INSERT INTO scan_run_phases (run_id, phase, seq, started_ts, finished_ts, status) VALUES
  ('r1', 'list b1', 0, ${NOW - 2 * DAY}, ${NOW - 2 * DAY + 300}, 'done'),
  ('r1', 'list b2', 1, ${NOW - 2 * DAY}, ${NOW - 2 * DAY + 200}, 'done'),
  ('r1', 'path-index', 2, ${NOW - 2 * DAY + 300}, ${NOW - 2 * DAY + 550}, 'done'),
  ('r2', 'list b1', 0, ${NOW - DAY}, ${NOW - DAY + 100}, 'done'),
  ('r2', 'digest', 1, ${NOW - DAY + 100}, ${NOW - DAY + 400}, 'failed');
`

/** The runs the card draws from `SEED`, newest first, as the fleet document keeps them. */
const RUNS: DeployData['runs'] = [
  { scan: '2026-11-21T1200', status: 'succeeded', trigger: 'manual', started: NOW - DAY, secs: 400, cost_usd: null, open_issues: ['digest'], families: [['list', 100], ['digest', 300]], bad: [['digest', 'failed']] },
  { scan: '2026-11-20T1200', status: 'succeeded', trigger: 'schedule', started: NOW - 2 * DAY, secs: 600, cost_usd: 1.25, open_issues: [], families: [['list', 300], ['path-index', 250], ['', 50]], bad: [] },
]

async function seeded(): Promise<DeployEnv> {
  const { db, raw } = await sqliteD1()
  raw.exec(SEED)
  return { DB: db, DISKY_LABELS: 'app=disky,deployment=gcs' } as DeployEnv
}

/** Two scans a day apart on the 20th, one on the 21st, and one 31 days back: the newest of each day within 30 days. */
const SERIES = [
  { t: NOW - 31 * DAY, bytes: 1e12, objects: 900 },
  { t: NOW - 2 * DAY - 3600, bytes: 2e12, objects: 1000 },
  { t: NOW - 2 * DAY, bytes: 2.1e12, objects: 1050 },
  { t: NOW - DAY, bytes: 2.2e12, objects: 1100 },
  { t: NOW - DAY + 60, bytes: null, objects: null },
]

afterEach(() => { vi.restoreAllMocks() })

describe('deployData', () => {
  it('reads the store\'s runs from D1, and its sizes from its series', async () => {
    const points = [{ t: NOW - DAY, bytes: 2.2e12, objects: 1100 }]
    expect(await deployData(await seeded(), ORIGIN, NOW, async () => points)).toEqual({ name: 'gcs', runs: RUNS, points })
  })
  it('keeps the newest scan of each day over 30 days, as the fleet does', () => {
    expect(dailyPoints(SERIES, NOW)).toEqual([
      { t: NOW - 2 * DAY, bytes: 2.1e12, objects: 1050 },
      { t: NOW - DAY, bytes: 2.2e12, objects: 1100 },
    ])
  })
  it('a D1 without the scan-run tables, or none, says so in the header; a series it can\'t read is an empty strip', async () => {
    const { db } = await sqliteD1()
    const noTables = { prepare: db.prepare.bind(db), batch: async () => { throw new Error('D1_ERROR: no such table: scan_runs') } }
    // no index store configured: `/api/series` answers 503, and the strip has no points
    expect(await deployData({ DB: noTables, ROOT_LABEL: 'my-bucket' } as never, ORIGIN, NOW)).toEqual({
      name: 'my-bucket', runs: [], points: [], runsError: 'scan runs are not set up',
    })
    expect(await deployData({} as never, ORIGIN, NOW, async () => [])).toEqual({ name: 'storage', runs: [], points: [], runsError: 'no D1 bound' })
  })
})

describe('the card\'s name, colour and version', () => {
  it('names the deployment, and a secondary store beside it', () => {
    expect(deployName({ DISKY_LABELS: 'app=disky,deployment=cw' } as DeployEnv)).toBe('cw')
    expect(deployName({ DISKY_LABELS: 'app=disky,deployment=cw', STORE_KEY: 'meta' } as DeployEnv)).toBe('cw · meta')
    expect(deployName({ ROOT_LABEL: 'my-bucket' } as DeployEnv)).toBe('my-bucket')
    expect(deployName({ DISKY_LABELS: 'not a label' } as DeployEnv)).toBe('storage')
  })
  it('takes a colour as six hex digits, `#` optional; anything else is the grey', () => {
    expect(['#EA4335', 'ea4335', '#ea433', 'red', null, ''].map(colorParam)).toEqual(['ea4335', 'ea4335', null, null, null, null])
    expect(DEFAULT_COLOR).toBe('#8b8a80')
  })
  it('signs the store (not the primary), the theme (light only), the colour and the version', () => {
    expect(deployParams({} as DeployEnv, {}, 'v1')).toEqual({ v: 'v1' })
    expect(deployParams({ STORE_KEY: 'meta' } as DeployEnv, { theme: 'light', c: 'ea4335' }, 'v1')).toEqual({ store: 'meta', theme: 'light', c: 'ea4335', v: 'v1' })
  })
  it('a version names the runs and sizes, but not a running run\'s clock', async () => {
    const data: DeployData = { name: 'gcs', runs: RUNS, points: [] }
    const running = (secs: number): DeployData => ({
      ...data,
      runs: [{ scan: '2026-11-22T1100', status: 'running', trigger: 'schedule', started: NOW - 3600, secs, cost_usd: null, open_issues: [], families: [['list', secs]], bad: [['list', 'running']] }, ...RUNS],
    })
    const [v, a, b, done] = await Promise.all([data, running(100), running(200), { ...data, runs: [{ ...running(100).runs[0], status: 'succeeded' }, ...RUNS] }].map(deployVersion))
    expect(v).toMatch(/^[0-9a-f]{12}$/)
    expect([a === b, a === v, done === a]).toEqual([true, false, false])
  })
})

describe('the signed URL', () => {
  it('is a full `/og/deploy.png` card through the end of its 7th day, with the view it was minted for', async () => {
    const key = await ogKey('secret')
    const env = await seeded()
    const { url, v, day } = await deployCardUrl(key, env, ORIGIN, NOW, { c: 'ea4335' })
    const want = await deployVersion(await deployData(env, ORIGIN, NOW))
    expect([v, url.replace(/&sig=\w+$/, '&sig=…')]).toEqual([want, `${ORIGIN}/og/deploy.png?c=ea4335&v=${want}&sig=…`])
    expect(await resolveImage(key, new URL(url), NOW)).toEqual({ kind: 'deploy', params: { c: 'ea4335', v: want }, tier: 'full', day })
    expect(await resolveImage(key, new URL(url), NOW + 8 * DAY)).toEqual({ kind: 'deploy', params: { c: 'ea4335', v: want }, tier: 'anon', why: 'expired' })
    expect(await resolveImage(key, new URL(url.replace('c=ea4335', 'c=000000')), NOW)).toEqual({ kind: 'deploy', params: { c: '000000', v: want }, tier: 'anon', why: 'bad signature' })
  })

  it('`GET /api/scan-runs/card-url` mints it, or says why it can\'t', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
    const env = await seeded()
    const get = (e: object, query = '') => cardUrlRoute({ request: new Request(`${ORIGIN}/api/scan-runs/card-url${query}`), env: e } as never)
    const refused = await get(env)
    expect([refused.status, await refused.json()]).toEqual([503, { error: 'card URLs not configured (SESSION_SECRET unset)' }])
    const res = await get({ ...env, SESSION_SECRET: 'secret' }, '?color=%23EA4335&theme=light')
    const body = await res.json() as { url: string; v: string; expires: number }
    const v = await deployVersion(await deployData(env, ORIGIN, NOW))
    expect([res.status, res.headers.get('cache-control'), body.v, body.url.replace(/&sig=\w+$/, '&sig=…'), body.expires]).toEqual([
      200, 'no-store', v, `${ORIGIN}/og/deploy.png?c=ea4335&theme=light&v=${v}&sig=…`, Date.UTC(2026, 10, 30) / 1000,
    ])
    expect((await resolveImage(await ogKey('secret'), new URL(body.url), NOW))?.tier).toBe('full')
  })
})
