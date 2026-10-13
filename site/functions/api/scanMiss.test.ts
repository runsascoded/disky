import { describe, expect, it, vi } from 'vitest'
import type { D1Database } from '@cloudflare/workers-types'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { indexedScan, scanArg } from '../_lib/scanArg'

// A scan id or `d=` slug naming no indexed scan is a 404 `{error}` on every
// scan-selecting API — never the nearest or latest scan instead.
vi.mock('../_lib/edgeCache.js', async orig => ({
  ...await orig<typeof import('../_lib/edgeCache.js')>(),
  // A resolved request answers as a cache hit carrying its key: no store read.
  cacheMatch: vi.fn(async (_env: unknown, key: Request) => new Response(JSON.stringify({ hit: new URL(key.url).pathname }))),
}))
// the ledger routes run on a lineage with the ledger (the cw D1 here has none)
vi.mock('../_lib/ledger.js', async orig => ({ ...await orig<typeof import('../_lib/ledger.js')>(), hasLedger: vi.fn(async () => true), ledgerHead: vi.fn(async () => 0) }))
// A date-only scan's start (`meta.started`), as the store would answer it: the 10/9 daily run's.
const STARTS: Record<string, string> = { '2026-10-09': '2026-10-09T04:30:12.345Z' }
vi.mock('../_lib/scanTimes.js', async orig => {
  const m = await orig<typeof import('../_lib/scanTimes.js')>()
  return { ...m, scanTimes: (e: Env, scans: readonly string[]) => m.scanTimes(e, scans, async (_e, id) => STARTS[id] ?? null) }
})
vi.mock('../_lib/extras.js', async orig => ({ ...await orig<typeof import('../_lib/extras.js')>(), hasExtras: vi.fn(async () => false) }))
vi.mock('../_lib/ownerTotals.js', () => ({ ownerTotals: vi.fn(async (_env: unknown, date: string) => ({ head: 0, users: {}, assignments: [], date })) }))
const { onRequestGet: subtree } = await import('./subtree')
const { onRequestGet: diff } = await import('./diff')
const { onRequestGet: owners } = await import('./owners')
const { onRequestGet: assignments } = await import('./assignments')

// Two scans on 10/9 (06:01, 12:36) and a date-only 10/8.
const SCANS = ['2026-10-08', '2026-10-09T0601', '2026-10-09T1236']
async function db(): Promise<D1Database> {
  const { db } = await sqliteD1('cw')
  for (const date of SCANS) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
  return db
}
const STORE = { GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data', DEV_EMAIL: 'alan@example.test' }
const env = async (): Promise<Env> => ({ ...STORE, DB: await db() }) as Env
const at = (e: Env, path: string) => ({ request: new Request(`http://localhost${path}`), env: e })
const answer = async (r: Response) => [r.status, await r.json()]

describe('scanArg — the Functions resolver', () => {
  it('indexedScan: an exact id only itself; a prefix its latest match', async () => {
    const e = await env()
    expect(await Promise.all([
      indexedScan(e, '2026-10-08', true), indexedScan(e, '2026-10-09', true), indexedScan(e, '2026-10-09T0601', true),
      indexedScan(e, '2026-10-09', false), indexedScan(e, '2026-10-09T06', false), indexedScan(e, '2026-10-09T03', false), indexedScan(e, '2026-10-10', false),
    ])).toEqual(['2026-10-08', null, '2026-10-09T0601', '2026-10-09T1236', '2026-10-09T0601', null, null])
  })
  it.each([
    ['date=2026-10-09T0601', '2026-10-09T0601'],
    ['d=261009', '2026-10-09T1236'],
    ['d=26100906', '2026-10-09T0601'],
    ['d=2610091236', '2026-10-09T1236'],
    ['d=261009-06', '2026-10-09T0601'],
    ['d=2026-10-09', '2026-10-09T1236'],
    ['d=261008', '2026-10-08'],
    // a date-only scan's exact slug is its midnight (and the midnight hour takes it)
    ['d=2610080000', '2026-10-08'],
    ['d=26100800', '2026-10-08'],
    // a date-only id is exact: the 10/9 timed scans don't answer for it
    ['date=2026-10-09', [404, { error: 'no scan matches date=2026-10-09' }]],
    ['d=26100903', [404, { error: 'no scan matches d=26100903' }]],
    ['d=261010', [404, { error: 'no scan matches d=261010' }]],
    ['d=junk', [404, { error: 'no scan matches d=junk' }]],
    ['date=261009', [400, { error: 'date=YYYY-MM-DD[THHMM] required' }]],
    ['', [400, { error: 'date=YYYY-MM-DD[THHMM] required' }]],
  ])('%s → %j', async (qs, want) => {
    const r = await scanArg(await env(), new URLSearchParams(qs))
    expect(r instanceof Response ? await answer(r) : r).toEqual(want)
  })
})

describe('a date-only and a timed scan on one day', () => {
  it('the day slug is the latest; each scan by its exact slug', async () => {
    const { db } = await sqliteD1('cw')
    for (const date of ['2026-10-09', '2026-10-09T1236']) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
    const e = { ...STORE, DB: db } as Env
    expect(await Promise.all(['d=261009', 'd=2610090000', 'd=2610091236', 'date=2026-10-09', 'date=2026-10-09T1236'].map(qs => scanArg(e, new URLSearchParams(qs)))))
      .toEqual(['2026-10-09T1236', '2026-10-09', '2026-10-09T1236', '2026-10-09', '2026-10-09T1236'])
  })
})

describe('a date-only scan sharing its day is keyed by its start (meta.started)', () => {
  it('its start minute and hour select it; the midnight stays an alias; the day is the latest', async () => {
    const { db } = await sqliteD1('cw')
    for (const date of ['2026-10-08', '2026-10-09', '2026-10-09T1236']) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
    const e = { ...STORE, DB: db } as Env
    const qs = ['d=2610090430', 'd=26100904', 'd=2610090000', 'd=261009', 'd=26100905', 'd=2610080000']
    const got = await Promise.all(qs.map(async q => { const r = await scanArg(e, new URLSearchParams(q)); return r instanceof Response ? await answer(r) : r }))
    expect(got).toEqual(['2026-10-09', '2026-10-09', '2026-10-09', '2026-10-09T1236', [404, { error: 'no scan matches d=26100905' }], '2026-10-08'])
  })
})

describe('the APIs answer a miss with 404 {error}', () => {
  it.each([
    ['/api/subtree?date=2026-10-09T0300&path=', [404, { error: 'no scan matches date=2026-10-09T0300' }]],
    ['/api/subtree?d=26100903&path=', [404, { error: 'no scan matches d=26100903' }]],
    ['/api/diff?from=2026-10-07&to=2026-10-09T1236&path=', [404, { error: 'no scan matches from=2026-10-07' }]],
    ['/api/diff?from=2026-10-08&to=2026-10-09T1200&path=', [404, { error: 'no scan matches to=2026-10-09T1200' }]],
  ])('%s', async (path, want) => {
    const handler = path.startsWith('/api/diff') ? diff : subtree
    expect(await answer(await handler(at(await env(), path) as never))).toEqual(want)
  })
  it('a slug that matches resolves to its latest scan (subtree keys the cache by the resolved id)', async () => {
    const [status, body] = await answer(await subtree(at(await env(), '/api/subtree?d=261009&path=') as never))
    expect([status, (body as { hit: string }).hit.split('/').slice(-2)]).toEqual([200, ['2026-10-09T1236', '']])
  })
  it.each([
    ['/api/owners?d=26100903', [404, { error: 'no scan matches d=26100903' }]],
    ['/api/assignments?date=2026-10-07', [404, { error: 'no scan matches date=2026-10-07' }]],
  ])('%s', async (path, want) => {
    const handler = path.startsWith('/api/owners') ? owners : assignments
    const e = await env()
    expect(await answer(await handler(at(e, path) as never))).toEqual(want)
  })
})
