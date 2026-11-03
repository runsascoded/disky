import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { fixture, GETS } from '../_lib/testStore'

// What a refused request can cost: with a session cookie that verifies (`AHEAD_COST`), the lookups a route starts beside
// its gate (`shared.ahead`) run to their end after the gate refuses it, on a cold isolate (nothing committed to join
// or read back), against counting R2 / D1 / KV / colo mocks over `fixtures/iv-churn/` (an interval store whose
// generation has 2 runs); with a forged or expired one, or a token, only the gate's own reads (`GATE_COST`). The gate is
// the real one.

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

const { onRequestGet: level, LEVEL_DIRS } = await import('./diff/level')
const { onRequestGet: diff } = await import('./diff')
const { onRequestGet: subtree } = await import('./subtree')
const { AHEAD_COST, GATE_COST } = await import('../_lib/aheadCost')
const { PLAN_AHEAD_FOOTERS } = await import('../_lib/diffLevel')
const { clearIsolate, loadScopes } = await import('../_lib/shared')
const { signSession } = await import('@open-athena/auth')
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

/** Each statement's wait before it runs (ms; negative: none, its answer a microtask away), so the gate refuses
 *  sooner or later into the lookups it runs beside. */
const d1Lag = { ms: -1 }
/** `db`, every statement run (alone or in a batch) counted, after `d1Lag`. */
function counted(db: D1Database): D1Database {
  const wrap = (st: D1PreparedStatement): D1PreparedStatement => new Proxy(st, {
    get(t, k) {
      const v = Reflect.get(t, k) as unknown
      if (k === 'bind') return (...a: unknown[]) => wrap((v as (...a: unknown[]) => D1PreparedStatement).apply(t, a))
      if (k === 'first' || k === 'all' || k === 'run' || k === 'raw') return async (...a: unknown[]) => {
        n.d1++
        if (d1Lag.ms >= 0) await new Promise(r => setTimeout(r, d1Lag.ms))
        return (v as (...a: unknown[]) => unknown).apply(t, a)
      }
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

const SECRET = 'test-secret'
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
    DB: counted(db), SESSION_SECRET: SECRET, BASE_SCOPE: 'laptop', ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g',
    INDEX_R2: r2('iv-churn'), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b',
    CACHE_KV: { get: async () => { n.kv++; return null }, put: async () => {} },
  } as unknown as Env
  reset()
  PLAN_AHEAD_FOOTERS.max = 16
})

/** A session cookie for `sub`, signed under `secret`, minted `ago` ms before now with a `ttl` (s). */
const session = async (sub: string, { secret = SECRET, ago = 0, ttl }: { secret?: string; ago?: number; ttl?: number } = {}) =>
  ({ cookie: `oa_auth=${await signSession(sub, secret, Date.now() - ago, ttl)}` })
/** A session that verifies (the gate's secret, unexpired) for an email the allowlist doesn't hold: the gate refuses it. */
const VALID = await session('e:removed@example.test')
/** The same session signed under another secret. */
const FORGED = await session('e:removed@example.test', { secret: 'not-the-secret' })
/** The same session, signed under the gate's secret, expired an hour ago. */
const EXPIRED = await session('e:removed@example.test', { ago: 2 * 3600_000, ttl: 3600 })
/** A well-formed token no grant holds. */
const TOKEN = { authorization: `Bearer ${'A'.repeat(32)}` }

/** `path`'s answer at a non-local host (the real gate) and what it read, once every lookup it started has settled:
 *  they run on past the refusal (`done()` discards their writes, it doesn't cancel them). */
async function cost(route: (c: { request: Request; env: Env }) => Promise<Response>, path: string, headers: Record<string, string> = VALID) {
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
/** Refused, having read `c` beside the gate, and the gate's own reads of the verifying session. */
const refused = (c: Counts) => ({ status: 401, ...c, d1: c.d1 + GATE_COST.session.d1 })

describe('a refused request whose session cookie verifies reads at most `AHEAD_COST`', () => {
  it('level: R2, KV and colo at their worst over held scans and every directory it may ask; D1 over unheld ones', async () => {
    const held = await cost(level, levelAt('2026-08-02', '2026-08-06'))
    // The fixture's plan over these directories reads one footer group a tier.
    const worst = AHEAD_COST.level({ dirs: LEVEL_DIRS, runs: RUNS, footers: 1 + RUNS })
    expect(held).toEqual(refused({ ...worst, d1: 0 }))
    reset()
    // Neither held: refused before any handle (the state and the base's footer indexes read), never a per-scan one.
    expect(await cost(level, levelAt(P1, P2))).toEqual(refused({ r2: 3 + 2, d1: worst.d1, kv: LEVEL_DIRS, colo: LEVEL_DIRS + 1 + 4 }))
  })

  it('level: the same whenever the gate refuses (its lookups, running on, read back what they read, never again)', async () => {
    const got = []
    for (const ms of [-1, 0, 1, 5, 20, 60]) {
      reset()
      d1Lag.ms = ms
      got.push(await cost(level, levelAt('2026-08-02', '2026-08-06')))
    }
    d1Lag.ms = -1
    const worst = AHEAD_COST.level({ dirs: LEVEL_DIRS, runs: RUNS, footers: 1 + RUNS })
    expect(got).toEqual(Array(6).fill(refused({ ...worst, d1: 0 })))
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

  it('a forged or expired session cookie, or a token, reads exactly `GATE_COST`: nothing starts beside the gate', async () => {
    const paths = [
      [level, levelAt('2026-08-02', '2026-08-06')],
      [diff, '/api/diff?from=2026-08-02&to=2026-08-06&path=&w=1280&h=768'],
      [subtree, '/api/subtree?date=2026-08-06&path=&w=1280&h=768'],
    ] as const
    const got: unknown[] = []
    for (const [route, path] of paths) {
      for (const headers of [FORGED, EXPIRED, TOKEN]) {
        reset()
        got.push(await cost(route, path, headers))
      }
    }
    const none = { r2: 0, kv: 0, colo: 0 }
    expect(got).toEqual(paths.flatMap(() => [
      { status: 401, ...none, d1: GATE_COST.cookie.d1 },
      { status: 401, ...none, d1: GATE_COST.cookie.d1 },
      { status: 401, ...none, d1: GATE_COST.token.d1 },
    ]))
    expect([GATE_COST.cookie.d1, GATE_COST.token.d1]).toEqual([0, 2])
  })
})
