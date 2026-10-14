import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { staticRegistry, staticSummary } from './nameSummaryStatic.js'
import { rollupAt, staticFilterStore, staticTag } from './staticFilter.js'
import { staticGen, staticPrefix } from './staticNames.js'
import { fixture } from './testStore.js'

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
    expect([s.gen, await s.scans(), staticTag(env, { ast: { neg: [], alts: [[{ kind: 'sub', text: 'qqq' }]] } as never })])
      .toEqual([GEN, ['2026-08-01', '2026-09-01', ...RUNS], `${GEN}.19`])
    expect(log.filter(k => !k.includes(`static-names/${GEN}/`))).toEqual([])
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
