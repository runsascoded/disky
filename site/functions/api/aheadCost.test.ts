import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { fixture, GETS } from '../_lib/testStore'

// What a refused request with a well-formed junk credential can cost (`AHEAD_COST`): the lookups a route starts beside
// its gate (`shared.ahead`) run to their end after the gate refuses it, on a cold isolate (nothing committed to join
// or read back), against counting R2 / D1 / KV / colo mocks over `fixtures/iv-churn/` (an interval store whose
// generation has 2 runs). The gate is the real one; its own reads are `GATE_COST`.

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

const { onRequestGet: level, LEVEL_DIRS } = await import('./diff/level')
const { onRequestGet: diff } = await import('./diff')
const { onRequestGet: subtree } = await import('./subtree')
const { AHEAD_COST, GATE_COST } = await import('../_lib/aheadCost')
const { PLAN_AHEAD_FOOTERS } = await import('../_lib/diffLevel')
const { clearIsolate, loadScopes } = await import('../_lib/shared')
const { coloPutsSettled } = await import('../_lib/index')
await loadScopes()

type Counts = { r2: number; d1: number; kv: number; colo: number }
let n: Counts
/** The fixture generation's runs. */
const RUNS = 2

type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
const fs = (await import(/* @vite-ignore */ 'node:' + 'fs')) as Fs

/** `INDEX_R2` over a fixture dir, every call (`get`, `head`, `list`) counted. */
function r2(dir: string): R2Bucket {
  const path = (key: string) => fixture(`${dir}/${key}`)
  const list = async ({ prefix }: { prefix: string }) => {
    n.r2++
    const d = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = path(d)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${d}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(path(k)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    n.r2++
    if (!fs.existsSync(path(key))) return null
    const buf = fs.readFileSync(path(key))
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  const head = async (key: string) => {
    n.r2++
    return fs.existsSync(path(key)) ? { key } : null
  }
  return { get, list, head } as unknown as R2Bucket
}

/** `db`, every statement run (alone or in a batch) counted. */
function counted(db: D1Database): D1Database {
  const wrap = (st: D1PreparedStatement): D1PreparedStatement => new Proxy(st, {
    get(t, k) {
      const v = Reflect.get(t, k) as unknown
      if (k === 'bind') return (...a: unknown[]) => wrap((v as (...a: unknown[]) => D1PreparedStatement).apply(t, a))
      if (k === 'first' || k === 'all' || k === 'run' || k === 'raw') return (...a: unknown[]) => { n.d1++; return (v as (...a: unknown[]) => unknown).apply(t, a) }
      return typeof v === 'function' ? (v as (...a: unknown[]) => unknown).bind(t) : v
    },
  })
  return {
    prepare: (sql: string) => wrap(db.prepare(sql)),
    batch: (sts: D1PreparedStatement[]) => { n.d1 += sts.length; return db.batch(sts) },
  } as unknown as D1Database
}

/** Two scans D1 points at (per-scan pointers: their files are not in the store) that the interval store doesn't hold. */
const [P1, P2] = ['2020-01-01', '2020-01-02']

let env: Env
const reset = () => {
  n = { r2: 0, d1: 0, kv: 0, colo: 0 }
  GETS.length = 0
  clearIsolate()
}
beforeEach(async () => {
  const { db, raw } = await sqliteD1()
  for (const d of [P1, P2]) raw.prepare(`INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES (?, 'path', 2, '{}', 'x', 'per-scan/${d}')`).run(d)
  ;(globalThis as unknown as { caches: unknown }).caches = {
    default: {
      match: async () => { n.colo++; return undefined },
      put: async () => {},
      delete: async () => true,
    },
  }
  env = {
    DB: counted(db), SESSION_SECRET: 'test-secret', BASE_SCOPE: 'laptop', ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g',
    INDEX_R2: r2('iv-churn'), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b',
    CACHE_KV: { get: async () => { n.kv++; return null }, put: async () => {} },
  } as unknown as Env
  reset()
  PLAN_AHEAD_FOOTERS.max = 16
})

/** A well-formed session cookie the gate can't verify (its signature is junk). */
const JUNK_COOKIE = { cookie: 'oa_auth=eyJzdWIiOiJlOngifQ.c2lnbmF0dXJlLW5vdC1yZWFs' }
/** A well-formed token no grant holds. */
const JUNK_TOKEN = { authorization: `Bearer ${'A'.repeat(32)}` }

/** `path`'s answer at a non-local host (the real gate) and what it read, once every lookup it started has settled:
 *  they run on past the refusal (`done()` discards their writes, it doesn't cancel them). */
async function cost(route: (c: { request: Request; env: Env }) => Promise<Response>, path: string, headers: Record<string, string> = JUNK_COOKIE) {
  const r = await route({ request: new Request(`http://example.test${path}`, { headers }), env })
  await r.text()
  for (let still = 0, last = ''; still < 5;) {
    await coloPutsSettled()
    await new Promise(res => setTimeout(res, 10))
    const now = JSON.stringify(n) + GETS.length
    still = now === last ? still + 1 : 0
    last = now
  }
  return { status: r.status, r2: n.r2 + GETS.length, d1: n.d1, kv: n.kv, colo: n.colo }
}

/** `LEVEL_DIRS` directories: the root, both buckets and the rest absent. */
const DIRS = ['', 'b1', 'b2', ...Array.from({ length: LEVEL_DIRS - 3 }, (_, i) => `d${i}`)]
const levelAt = (from: string, to: string) => `/api/diff/level?from=${from}&to=${to}${DIRS.map(p => `&p=${p}`).join('')}`
const refused = (c: Counts) => ({ status: 401, ...c })

describe('a refused request with a well-formed junk credential reads at most `AHEAD_COST`', () => {
  it('level: R2, KV and colo at their worst over held scans and every directory it may ask; D1 over unheld ones', async () => {
    const held = await cost(level, levelAt('2026-08-02', '2026-08-06'))
    // The fixture's plan over these directories reads one footer group a tier.
    const worst = AHEAD_COST.level({ dirs: LEVEL_DIRS, runs: RUNS, footers: 1 + RUNS })
    expect(held).toEqual(refused({ ...worst, d1: 0 }))
    reset()
    // Neither held: refused before any handle (the state and the base's footer indexes read), never a per-scan one.
    expect(await cost(level, levelAt(P1, P2))).toEqual(refused({ r2: 3 + 2, d1: worst.d1, kv: LEVEL_DIRS, colo: LEVEL_DIRS + 1 + 4 }))
  })

  it('level: the early plan reads at most `PLAN_AHEAD_FOOTERS.max` footer groups (a wider one is dropped: the read plans again)', async () => {
    const at = async (max: number) => {
      reset()
      PLAN_AHEAD_FOOTERS.max = max
      return cost(level, levelAt('2026-08-02', '2026-08-06'))
    }
    // The plan wants one a tier (3): under 3 it is refused, its tiers having read what fit.
    expect([await at(RUNS), await at(0)]).toEqual([RUNS, 0].map(footers => refused({ ...AHEAD_COST.level({ dirs: LEVEL_DIRS, runs: RUNS, footers }), d1: 0 })))
  })

  it('diff: over held scans, and over unheld ones', async () => {
    const path = (from: string, to: string) => `/api/diff?from=${from}&to=${to}&path=&w=1280&h=768`
    expect(await cost(diff, path('2026-08-02', '2026-08-06'))).toEqual(refused({ ...AHEAD_COST.diff, d1: 0 }))
    reset()
    expect(await cost(diff, path(P1, P2))).toEqual(refused(AHEAD_COST.diff))
  })

  it('subtree: over a held scan, and over a `d=<slug>` resolving to an unheld one', async () => {
    expect(await cost(subtree, '/api/subtree?date=2026-08-06&path=&w=1280&h=768')).toEqual(refused({ ...AHEAD_COST.subtree, d1: 0 }))
    reset()
    expect(await cost(subtree, '/api/subtree?d=200102&path=&w=1280&h=768')).toEqual(refused(AHEAD_COST.subtree))
  })

  it('the gate\'s own reads: none for a session cookie it can\'t verify, a lookup and a logged denial for a token', async () => {
    expect(await cost(subtree, '/api/subtree?date=2026-08-06&path=&w=1280&h=768', JUNK_TOKEN)).toEqual(refused({ ...AHEAD_COST.subtree, d1: GATE_COST.token.d1 }))
    expect(GATE_COST.cookie.d1).toBe(0)
  })
})
