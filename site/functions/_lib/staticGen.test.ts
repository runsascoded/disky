import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { staticRegistry, staticSummary } from './nameSummaryStatic.js'
import { rollupAt, staticFilterStore, staticTag } from './staticFilter.js'
import { staticGen, staticPrefix } from './staticNames.js'
import { fixture } from './testStore.js'
import { storeEnv, withStore } from './stores.js'
import { IV_STATE_TTL, withPathStore } from './index.js'
import type { Env } from './auth.js'

// The generation is required deployment config (`STATIC_GEN`): every read of a deployment's static name index is under its own
// `static-names/<gen>/` in `INDEX_R2`, and the responses name it. The bucket here holds `fixtures/static-runs/` (a base
// and a run per scan, the last two on one day) as cw's generation `2026-10-09cw`.

const GEN = '2026-10-09cw'
const BUCKETS = 'bkt-a,bkt-b'
const RUNS = ['2026-10-01', '2026-10-02', '2026-10-03T0600', '2026-10-03T1800']
const FILES = ['shards.json', 'sx/s0000.parquet', 'sx/s0001.parquet', 'catalog/cells.parquet', 'catalog/index.parquet', 'catalog/meta.json']
const held = new Map<string, ArrayBuffer>()
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  const keys = [...['', ...RUNS.map(r => `deltas/${r}/`)].flatMap(t => FILES.map(f => `${t}${f}`)), 'scans.json', ...RUNS.map(r => `manifests/${r}.json`)]
  for (const k of keys) {
    const src = k.startsWith('deltas/') || k.startsWith('manifests/') ? k : `base/${k}`
    const b = fs.readFileSync(fixture(`static-runs/${src}`))
    held.set(`${staticPrefix(GEN)}/${k}`, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer)
  }
})

/** An R2 binding over `held`, recording every key read or listed. */
function r2(log: string[]): R2Bucket {
  const object = (key: string, body: ArrayBuffer, size: number) => ({ key, size, arrayBuffer: async () => body, json: async () => JSON.parse(new TextDecoder().decode(body)) })
  return {
    async get(key: string, opts?: { range?: { offset?: number; length?: number; suffix?: number } }) {
      log.push(key)
      const b = held.get(key)
      if (!b) return null
      const r = opts?.range
      if (!r) return object(key, b, b.byteLength)
      if (r.suffix != null) return object(key, b.slice(Math.max(0, b.byteLength - r.suffix)), b.byteLength)
      const lo = r.offset ?? 0
      return object(key, b.slice(lo, r.length == null ? undefined : lo + r.length), b.byteLength)
    },
    async list({ prefix }: { prefix: string }) {
      log.push(`list ${prefix}`)
      return { objects: [...held.keys()].filter(k => k.startsWith(prefix)).map(key => ({ key })), truncated: false }
    },
  } as unknown as R2Bucket
}

beforeEach(() => { vi.stubGlobal('caches', { default: { match: async () => undefined, put: async () => undefined } }) })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('STATIC_GEN', () => {
  it('is the deployment\'s generation, required (no deployment is assumed); one path segment', () => {
    expect([staticGen({ STATIC_GEN: GEN }), staticPrefix(GEN)]).toEqual([GEN, 'static-names/2026-10-09cw'])
    for (const env of [{}, { STATIC_GEN: ' ' }]) expect(() => staticGen(env)).toThrow('static names: STATIC_GEN is unset (the static name index generation in INDEX_R2)')
    expect(() => staticGen({ STATIC_GEN: 'a/b' })).toThrow('static names: bad STATIC_GEN "a/b"')
  })

  it('serves the registry and a summary from the configured generation alone', async () => {
    const log: string[] = []
    const env = { NAME_SUMMARY_STATIC: '1', INDEX_R2: r2(log), STATIC_GEN: GEN, STORE_BUCKETS: BUCKETS, STORE: 'cw' }
    const reg = await (await staticRegistry(env)).json() as { generation: string; dates: string[]; logical_store: string }
    expect([reg.generation, reg.dates, reg.logical_store]).toEqual([GEN, ['2026-08-01', '2026-09-01', ...RUNS], 'cw_fleet'])
    const res = await staticSummary(env, new URLSearchParams({ name: 'qqq', date: '2026-10-02' }))
    const body = await res.json() as { source_identity: { generation: string }; buckets: { path: string; b: number; o: number }[] }
    expect([res.status, body.source_identity.generation, body.buckets.map(b => [b.path, b.b, b.o]), JSON.parse(res.headers.get('x-static-io')!).gen])
      .toEqual([200, GEN, [['bkt-a', 30 + 32 + 34, 3], ['bkt-b', 31 + 33, 2]], GEN])
    expect(log.filter(k => !k.startsWith(`static-names/${GEN}/`) && !k.startsWith(`list static-names/${GEN}/`))).toEqual([])
  })

  it('answers a scan by its id, two scans of one day apart; a date slug is that day\'s latest scan', async () => {
    const env = { NAME_SUMMARY_STATIC: '1', INDEX_R2: r2([]), STATIC_GEN: GEN, STORE_BUCKETS: BUCKETS, STORE: 'cw' }
    const ask = async (q: Record<string, string>) => {
      const res = await staticSummary(env, new URLSearchParams({ name: 'sub-', ...q }))
      const body = await res.json() as { date?: string; from?: string; buckets: { path: string; b: number; o: number; after?: { b: number; o: number } }[]; delta?: { b: number; o: number } }
      return [res.status, body.from ?? null, body.date, body.buckets.map(b => [b.path, b.after?.b ?? b.b, b.after?.o ?? b.o]), body.delta ?? null]
    }
    expect(await ask({ date: '2026-10-03T0600' })).toEqual([200, null, '2026-10-03T0600', [['bkt-a', 7, 1], ['bkt-b', 0, 0]], null])
    expect(await ask({ date: '2026-10-03T1800' })).toEqual([200, null, '2026-10-03T1800', [['bkt-a', 0, 0], ['bkt-b', 9, 1]], null])
    expect(await ask({ date: '2026-10-03' })).toEqual([200, null, '2026-10-03T1800', [['bkt-a', 0, 0], ['bkt-b', 9, 1]], null])
    expect(await ask({ from: '2026-10-03T0600', date: '2026-10-03T1800' }))
      .toEqual([200, '2026-10-03T0600', '2026-10-03T1800', [['bkt-a', 0, 0], ['bkt-b', 9, 1]], { b: 2, o: 0 }])
    const missing = await staticSummary(env, new URLSearchParams({ name: 'sub-', date: '2026-10-04T0000' }))
    expect([missing.status, await missing.json()]).toEqual([400, { error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }])
  })

  it('keys the map filter\'s store, its scans and its cache tag by the configured generation', async () => {
    const log: string[] = []
    const env = { FILTER_STATIC: '1', INDEX_R2: r2(log), STATIC_GEN: GEN }
    const s = staticFilterStore(env)!
    const term = (text: string) => ({ ast: { neg: [], alts: [[{ kind: 'sub', text }]] } as never })
    expect([s.gen, await s.scans(), staticTag(env, term('qqq')), staticTag(env, term('a/bc')), staticTag({ ...env, SEARCH_PATH_TERMS: '1' }, term('a/bc')), staticTag({ ...env, SEARCH_PATH_TERMS: '1' }, term('qqq'))])
      .toEqual([GEN, ['2026-08-01', '2026-09-01', ...RUNS], `${GEN}.31`, `${GEN}.31`, `${GEN}.31.p`, `${GEN}.31`])
    expect(log.filter(k => !k.includes(`static-names/${GEN}/`))).toEqual([])
  })

  it('a secondary store reads a static index only when its own config names one: none holds no scan, never the primary\'s generation', async () => {
    const log: string[] = []
    const env = { FILTER_STATIC: '1', INDEX_R2: r2(log), STATIC_GEN: GEN, FILTER_STATIC_HEAVY: '1' } as Env
    const bare = staticFilterStore(storeEnv(env, 'meta', {}))!
    const own = staticFilterStore(storeEnv(env, 'meta', { vars: { STATIC_GEN: GEN } }))!
    const primary = staticFilterStore(env)!
    expect([bare.gen, await bare.scans(), await bare.source.hits('qqq', ''), own.gen, await own.scans(), own.source.heavy, primary.gen, primary.source.heavy])
      .toEqual(['none', [], null, GEN, ['2026-08-01', '2026-09-01', ...RUNS], false, `${GEN}+drill`, true])
  })

  it('with no drilldown (`FILTER_STATIC_HEAVY` unset), a heavy literal\'s fleet root is the catalog\'s buckets — `/api/name-summary`\'s numbers — and below it nothing', async () => {
    const env = { FILTER_STATIC: '1', INDEX_R2: r2([]), STATIC_GEN: GEN, NAME_SUMMARY_STATIC: '1', STORE_BUCKETS: BUCKETS, STORE: 'cw' }
    const s = staticFilterStore(env)!
    const got: unknown[] = []
    const want: unknown[] = []
    for (const date of ['2026-09-01', '2026-10-03T1800']) {
      const root = await s.source.hits('su', '')
      const { kids, rest } = rollupAt(root!.rollup!, date)
      got.push([date, root!.rollup!.bucketsOnly, root!.io.from, kids.map(([c, b, o]) => [c, Number(b), Number(o)]), rest])
      const summary = await (await staticSummary(env, new URLSearchParams({ name: 'su', date }))).json() as { plan: string; buckets: { path: string; b: number; o: number }[] }
      want.push([date, true, 'catalog', summary.buckets.filter(b => b.b || b.o).map(b => [b.path, b.b, b.o]), [0n, 0n]])
      expect(summary.plan).toBe('catalog')
    }
    expect(got).toEqual(want)
    expect([s.source.heavy, await s.source.hits('su', 'bkt-a')]).toEqual([false, null])
  })
})

// The compactor (`compact_store`, specs/static-compaction.md) moves `static-names/CURRENT.json` (`{"gen": …}`) once a
// compacted generation is whole on R2. Unpinned, the primary's generation is the pointer's, resolved once per request
// where the interval store's is (`withPathStore`) and held `IV_STATE_TTL` per isolate; a pin wins, and a secondary
// store keeps its own `STATIC_GEN` (none: `UNINDEXED`), never the primary's pointer.
describe('static-names/CURRENT.json', () => {
  const CURRENT = 'static-names/CURRENT.json'
  /** `r2(log)` with the pointer: `ptr.gen` what it holds (null: absent), `ptr.fail` a read that fails. */
  function pointed(log: string[], ptr: { gen: string | null; fail?: boolean }): R2Bucket {
    const base = r2(log)
    return {
      ...base,
      get: async (key: string, o?: unknown) => {
        if (key !== CURRENT) return (base.get as (k: string, o?: unknown) => Promise<unknown>)(key, o)
        log.push(key)
        if (ptr.fail) throw new Error('R2 down')
        return ptr.gen === null ? null : { key, json: async () => ({ gen: ptr.gen }) }
      },
    } as unknown as R2Bucket
  }
  const STORES_JSON = JSON.stringify({ meta: { vars: { STATIC_GEN: '2026-10-14-meta' } }, bare: { vars: {} } })
  /** The env a request's readers see: its store (`withStore`), then the path store's rule (`withPathStore`). */
  async function seen(env: object, q = ''): Promise<Env> {
    const c = withStore({ request: new Request(`https://x/api/subtree?date=2026-10-02${q}`), env: env as Env })
    if (c instanceof Response) throw new Error(`withStore: ${c.status}`)
    return (await withPathStore(c)).env
  }
  const reads = (log: string[]) => log.filter(k => k === CURRENT).length

  it('an explicit secondary pointer is independent of the primary pointer and cleared on another store', async () => {
    vi.spyOn(Date, 'now').mockImplementation(() => Date.UTC(2032, 0, 1))
    const log: string[] = []
    const bucket = { get: async (key: string) => {
      log.push(key)
      return { json: async () => ({ gen: key === 'static-names/test-primary-CURRENT.json' ? 'primary' : 'meta.c001' }) }
    } } as unknown as R2Bucket
    const env = { FILTER_STATIC: '1', INDEX_R2: bucket, STATIC_CURRENT_KEY: 'static-names/test-primary-CURRENT.json', STORES_JSON: JSON.stringify({ meta: { vars: { STATIC_CURRENT_KEY: 'static-names/test-meta-CURRENT.json' } }, bare: {} }) }
    expect([(await seen(env, '&store=meta')).STATIC_GEN, (await seen(env)).STATIC_GEN, (await seen(env, '&store=bare')).STATIC_GEN]).toEqual(['meta.c001', 'primary', undefined])
    expect(log).toEqual(['static-names/test-meta-CURRENT.json', 'static-names/test-primary-CURRENT.json'])
  })

  it('unpinned: the pointer\'s generation, held `IV_STATE_TTL`, a move followed after it; a pin wins; a failed read keeps the last; none = unset', async () => {
    let t = Date.now() + 10 * IV_STATE_TTL
    vi.spyOn(Date, 'now').mockImplementation(() => t)
    const expire = () => { t += IV_STATE_TTL }
    const log: string[] = []
    const ptr: { gen: string | null; fail?: boolean } = { gen: GEN }
    const env = { NAME_SUMMARY_STATIC: '1', FILTER_STATIC: '1', INDEX_R2: pointed(log, ptr), STORE_BUCKETS: BUCKETS, STORE: 'cw', STORES_JSON }
    const e1 = await seen(env)
    const reg = await (await staticRegistry(e1)).json() as { generation: string }
    expect([e1.STATIC_GEN, reg.generation, staticFilterStore(e1)!.gen, reads(log)]).toEqual([GEN, GEN, GEN, 1])
    // a pin wins, without reading the pointer
    expect([(await seen({ ...env, STATIC_GEN: 'pinned' })).STATIC_GEN, reads(log)]).toEqual(['pinned', 1])
    // a compaction moves the pointer: served once the held one is `IV_STATE_TTL` old
    ptr.gen = '2026-10-09cw.c001'
    expect([(await seen(env)).STATIC_GEN, reads(log)]).toEqual([GEN, 1])
    expire()
    expect([(await seen(env)).STATIC_GEN, reads(log)]).toEqual(['2026-10-09cw.c001', 2])
    // a failed read keeps serving the last pointer
    ptr.fail = true
    expire()
    expect([(await seen(env)).STATIC_GEN, reads(log)]).toEqual(['2026-10-09cw.c001', 3])
    // no pointer: exactly an unset `STATIC_GEN`
    ptr.fail = false
    ptr.gen = null
    expire()
    const off = await seen(env)
    expect([off.STATIC_GEN, reads(log)]).toEqual([undefined, 4])
    expect(() => staticFilterStore(off)).toThrow('static names: STATIC_GEN is unset (the static name index generation in INDEX_R2)')
  })

  it('a deployment not reading the static index (no `FILTER_STATIC` / `NAME_SUMMARY_STATIC`) never reads the pointer', async () => {
    vi.spyOn(Date, 'now').mockImplementation(() => Date.UTC(2031, 0, 1))
    const log: string[] = []
    const e = await seen({ INDEX_R2: pointed(log, { gen: GEN }) })
    expect([e.STATIC_GEN, reads(log)]).toEqual([undefined, 0])
  })

  it('a secondary store keeps its own pin, or none (`UNINDEXED`): never the primary\'s pointer', async () => {
    vi.spyOn(Date, 'now').mockImplementation(() => Date.UTC(2030, 0, 1))
    const log: string[] = []
    const env = { FILTER_STATIC: '1', INDEX_R2: pointed(log, { gen: GEN }), STORES_JSON }
    const meta = await seen(env, '&store=meta')
    const bare = await seen(env, '&store=bare')
    expect([meta.STATIC_GEN, bare.STATIC_GEN, staticFilterStore(bare)!.gen, reads(log)]).toEqual(['2026-10-14-meta', undefined, 'none', 0])
    // …and the primary, in the same isolate, follows it
    expect([(await seen(env)).STATIC_GEN, reads(log)]).toEqual([GEN, 1])
  })
})
