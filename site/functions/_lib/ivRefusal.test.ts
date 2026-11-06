// What decides which store answers a scan is never "not held" by failure: the interval store's generation pointer
// (`CURRENT.json`) and its state (`scans.json` + the newest manifest). A held scan's per-scan sorts are retired, so a
// failed read that fell through to them answered with their absence ("index variant 'path' not synced … (retired from
// D1; …)") instead of the store's own failure. And a scan the generation really doesn't hold (a site pinned to a
// generation behind the store's, gcs's dev on 2026-11-05) says so, naming the generation.
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { intervalsOn, IV_CURRENT, IV_STATE_TTL, ivRefusal, withPathStore } from './index'
import { prefixesAt } from './prefixes'
import { clearIsolate } from './shared'
import { type Sqlite, sqliteD1 } from './testD1'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))
const stampPage = vi.fn(async (_env: unknown, _url: URL, res: Response) => res)
const anonymousPage = vi.fn(async (): Promise<Response | null> => null)
vi.mock('./og/serve.js', () => ({ stampPage, anonymousPage }))
const { onRequest } = await import('../_middleware')

const GEN = 'g'
const HELD = '2026-01-01'
/** A scan the generation's newest manifest adds as a run. */
const RUN = '2026-01-02'
const PREFIX = 'gs://bk/a/'
const SCANS = { scans: [{ id: HELD, ts: 1767225600 }], slices: false }
const MANIFEST = { gen: GEN, date: RUN, scans: [HELD, RUN], stamps: { [RUN]: 1767312000 }, runs: [{ key: `deltas/${RUN}`, first: RUN, last: RUN, level: 0, scans: [RUN] }] }

/** `INDEX_R2` over `objects` (key → its JSON; an `Error` is thrown by the read; absent → null), listing `listed` too. */
function bucket(objects: () => Record<string, unknown>, listed: string[] = []): R2Bucket {
  return {
    get: async (key: string) => {
      const o = objects()[key]
      if (o instanceof Error) throw o
      return o === undefined ? null : { json: async () => o }
    },
    list: async ({ prefix }: { prefix: string }) => ({ objects: [...Object.keys(objects()), ...listed].filter(k => k.startsWith(prefix)).sort().map(key => ({ key })), truncated: false }),
  } as unknown as R2Bucket
}

let raw: Sqlite
let db: D1Database
/** The D1 statements a read prepared (a per-scan read starts with an `index_schema` lookup). */
let prepared: string[]
beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const d1 = await sqliteD1()
  raw = d1.raw
  db = { ...d1.db, prepare: (sql: string) => { prepared.push(sql.replace(/\s+/g, ' ').slice(0, 40)); return d1.db.prepare(sql) } } as D1Database
  // `RUN`'s per-scan pointer, its row groups and footer files retired (`dt-cloud scan retire`).
  raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES ('${RUN}', 'path', 2, '[]', 'G', 'listing/${RUN}/index/G')`)
})
beforeEach(() => {
  clearIsolate()
  prepared = []
})

/** `/api/prefixes`' read: the request's path store, then its prefixes at `date`. */
const sized = (env: Env, date: string) =>
  withPathStore({ request: new Request('https://x/api/prefixes'), env }).then(c => prefixesAt(c.env, date, [PREFIX]))
/** How a read ended: its interval-store refusal (`ivRefusal`), else that it answered or what it threw. */
async function outcome(p: Promise<unknown>): Promise<unknown> {
  const e = await p.then(() => null, (e: unknown) => e)
  if (e === null) return 'answered'
  const r = ivRefusal(e)
  return r ? [r.status, r.headers.get('retry-after'), r.headers.get('cache-control'), await r.text()] : { threw: (e as Error).message }
}
const RETIRED = `index variant 'path' not synced for ${RUN} (retired from D1; no listing/${RUN}/index/G/path-index.groups.json: no such key: listing/${RUN}/index/G/path-index.groups.json)`

describe('a failed read of what decides the store is the store\'s refusal', () => {
  it('the generation pointer, none held: a 503 naming it, no per-scan read; then served again, and kept through a later failure', async () => {
    let ptr: unknown = new Error('Network connection lost.')
    const env = { DB: db, PATH_STORE: 'intervals', INDEX_R2: bucket(() => ({ [IV_CURRENT]: ptr, [`interval-store/${GEN}/scans.json`]: SCANS, [`interval-store/${GEN}/manifests/${RUN}.json`]: MANIFEST })) } as Env
    let t = Date.now()
    const spy = vi.spyOn(Date, 'now').mockImplementation(() => t)
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect([await outcome(sized(env, RUN)), prepared]).toEqual([
        [503, '1', 'no-store', `interval store pointer ${IV_CURRENT} is unreadable: Network connection lost.; retry in 1s\n`],
        [],
      ])
      // the failed read isn't held: the next request reads the pointer again
      ptr = { gen: GEN }
      const on = (await withPathStore({ request: new Request('https://x/api/prefixes'), env })).env
      expect([on.INTERVAL_STORE_GEN, intervalsOn(on)]).toEqual([GEN, true])
      // a re-read that fails keeps the pointer the isolate holds
      t += IV_STATE_TTL
      ptr = new Error('Network connection lost.')
      expect((await withPathStore({ request: new Request('https://x/api/prefixes'), env })).env.INTERVAL_STORE_GEN).toEqual(GEN)
      // no pointer object (a store without one yet) is no store, as before: not a failure
      clearIsolate()
      ptr = undefined
      const off = (await withPathStore({ request: new Request('https://x/api/prefixes'), env })).env
      expect([off.INTERVAL_STORE_GEN, intervalsOn(off)]).toEqual([undefined, false])
    } finally {
      spy.mockRestore()
      logged.mockRestore()
    }
  })

  it('the generation\'s state, none held: a 503 naming it, no per-scan read', async () => {
    const env = { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: GEN, INDEX_R2: bucket(() => ({ [`interval-store/${GEN}/scans.json`]: new Error('internal error') })) } as Env
    expect([await outcome(sized(env, RUN)), prepared]).toEqual([
      [503, '1', 'no-store', `interval store state interval-store/${GEN} is unreadable: internal error; retry in 1s\n`],
      [],
    ])
  })

  it('a manifest listed and not found is a failed state, not a generation without runs (whose scans would read per-scan)', async () => {
    const env = { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: GEN, INDEX_R2: bucket(() => ({ [`interval-store/${GEN}/scans.json`]: SCANS }), [`interval-store/${GEN}/manifests/${RUN}.json`]) } as Env
    expect([await outcome(sized(env, RUN)), prepared]).toEqual([
      [503, '1', 'no-store', `interval store state interval-store/${GEN} is unreadable: interval-store/${GEN}/manifests/${RUN}.json is listed and not found; retry in 1s\n`],
      [],
    ])
  })
})

describe('a scan the generation doesn\'t hold', () => {
  it('whose per-scan index is retired says the generation doesn\'t hold it: not a refusal to retry', async () => {
    // A generation frozen at `HELD` (a pin behind the store's current one): `RUN` isn't in it, and reads per-scan.
    const env = { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: GEN, INDEX_R2: bucket(() => ({ [`interval-store/${GEN}/scans.json`]: SCANS })) } as Env
    expect(await outcome(sized(env, RUN))).toEqual({ threw: `scan ${RUN} is not in interval store generation ${GEN} and reads per-scan: ${RETIRED}` })
    // without an interval store the per-scan reader's own words stand
    expect(await outcome(sized({ DB: db } as Env, RUN))).toEqual({ threw: RETIRED })
  })
})

describe('the middleware', () => {
  const served = async (next: () => Promise<Response>) => {
    const res = await onRequest({ request: new Request('https://x/api/prefixes', { method: 'POST' }), env: {} as never, data: {}, next })
    return [res.status, res.headers.get('retry-after'), res.headers.get('cache-control'), await res.text()]
  }
  it('answers a refusal a route let escape (`withPathStore` runs before a route\'s own handling) as its 503; any other error stays thrown', async () => {
    const env = { DB: db, PATH_STORE: 'intervals', INDEX_R2: bucket(() => ({ [IV_CURRENT]: new Error('Network connection lost.') })) } as Env
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(await served(async () => { await withPathStore({ request: new Request('https://x/api/prefixes'), env }); return new Response('ok') })).toEqual(
        [503, '1', 'no-store', `interval store pointer ${IV_CURRENT} is unreadable: Network connection lost.; retry in 1s\n`],
      )
    } finally {
      logged.mockRestore()
    }
    await expect(served(async () => { throw new Error('boom') })).rejects.toEqual(new Error('boom'))
  })
})
