import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { AGE_EDGES_DAYS, bucketOf, completed, datedAges, scanDay, strata, withChildAges } from './childAges'
import { openIndex, readAt, readUnder } from './index'
import { sqliteD1 } from './testD1'
import { type D1Variant, readJson, seedGeneration } from './testStore'
import { buildView, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

/** The `v2-ages` fixture's scan (`fixtures/gen.py` `AGES_ASOF`). */
const DATE = '2026-10-18T1014'
const files = {
  path: { parquet: 'v2-ages/path-index.parquet', groups: 'v2-ages/path-index.groups.json' },
  'age-pyramid-1d': { parquet: 'v2-ages/age-pyramid-1d.parquet', groups: 'v2-ages/age-pyramid-1d.groups.json' },
}
let env: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1()
  const variants = await readJson<Record<string, D1Variant>>('v2-ages/d1.json')
  seedGeneration(raw, { date: DATE, gen: 'g1', dir: `listing/laptop/${DATE}/index/g1`, variants, files })
  env = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

/** The per-scan view of `path`, one level deep: its top node and children carry the `age_b*` columns (`ag`). */
const perScanView = async (path: string): Promise<ViewNode> =>
  (await buildView(env, { date: DATE, path, w: 4000, h: 4000, minArea: 1, atten: 1, threshold: 1, maxDepth: 1 })).tree

/** A node and its children's bars, `[name, ag]`. */
const bars = (t: ViewNode) => [[t.n, t.ag], ...(t.c ?? []).map(k => [k.n, k.ag])]

/** `t` as the interval store answers it: no `ag` anywhere. */
const unaged = (t: ViewNode): ViewNode => ({ ...t, ag: undefined, c: t.c?.map(k => ({ ...k, ag: undefined })) })

describe('bucketOf', () => {
  it('buckets by `AGE_EDGES_DAYS`, a future day the newest, as `age_bucket_sql`', () => {
    expect([-3, 0, 1, 6, 7, 29, 30, 90, 91, 364, 365, 1094, 1095, 4000].map(bucketOf)).toEqual([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6])
    expect(AGE_EDGES_DAYS).toEqual([1, 7, 30, 91, 365, 1095])
  })
})

describe('strata', () => {
  it('sums each path’s day bins into its buckets at the scan day', () => {
    const day = scanDay(DATE)
    const bin = (age: number) => (day - age) * 86_400_000
    expect([...strata([
      { path: 'bk/a', binstart: bin(0), b: 5 },
      { path: 'bk/a', binstart: bin(400), b: 7 },
      { path: 'bk/b', binstart: bin(-2), b: 3 },
    ], day)]).toEqual([['bk/a', [5, 0, 0, 0, 0, 7, 0]], ['bk/b', [3, 0, 0, 0, 0, 0, 0]]])
  })
})

describe('withChildAges', () => {
  it('gives the interval store’s view exactly the per-scan view’s bars, undated bytes in the oldest', async () => {
    for (const path of ['', 'bk', 'bk/deep', 'bk/deep/x']) {
      const want = await perScanView(path)
      expect({ path, bars: bars(await withChildAges(env, DATE, path, unaged(want))) }).toEqual({ path, bars: bars(want) })
    }
  })

  it('splits each edge on the right side (created hours within the UTC day, one future stamp)', async () => {
    const got = await withChildAges(env, DATE, 'bk', unaged(await perScanView('bk')))
    expect(got.c!.find(k => k.n === 'mixed')!.ag).toEqual([
      1000 + 777, 1001 + 1006, 1007 + 1029, 1030 + 1090, 1091 + 1364, 1365 + 2094, 2095 + 5000,
    ])
  })

  it('folds `(other)` as the node less its kept children, and leaves a child without strata bare', async () => {
    const node = (n: string, b: number, c?: ViewNode[]): ViewNode => ({ n, k: 'dir', b, o: 1, ...(c ? { c } : {}) })
    // `(other)` holds `deep` and `new`: their per-scan columns summed.
    const got = await withChildAges(env, DATE, 'bk', node('bk', 64116, [node('mixed', 20949), node('old', 30999), node('(other)', 12168)]))
    expect(bars(got)).toEqual([
      ['bk', [2141, 7731, 2228, 6472, 2583, 3843, 39118]],
      ['mixed', [1777, 2007, 2036, 2120, 2455, 3459, 7095]],
      ['old', [0, 0, 0, 0, 0, 0, 30999]],
      ['(other)', [64 + 300, 4224 + 1500, 192, 4352, 128, 384, 1024]],
    ])
    const bare = await withChildAges(env, DATE, 'bk', node('bk', 64116, [node('mixed', 20949), node('unknown', 1), node('(other)', 43166)]))
    expect(bars(bare).map(([n, ag]) => [n, !!ag])).toEqual([['bk', true], ['mixed', true], ['unknown', false], ['(other)', false]])
  })

  it('is the tree unchanged for a scan without a pyramid', async () => {
    const t: ViewNode = { n: 'bk', k: 'dir', b: 1, o: 1, c: [{ n: 'mixed', k: 'dir', b: 1, o: 1 }] }
    expect(await withChildAges(env, '2026-10-17T2207', 'bk', t)).toBe(t)
    expect(await datedAges(env, '2026-10-17T2207', 'bk')).toBe(null)
  })
})

describe('completed', () => {
  it('puts what the newer buckets leave in the oldest', () => {
    expect(completed([1, 2, 3, 4, 5, 6, 7], 100)).toEqual([1, 2, 3, 4, 5, 6, 79])
  })
})

describe('readUnder', () => {
  it('reads a node’s children only; past its group budget, nothing', async () => {
    const h = await openIndex(env, DATE, 'age-pyramid-1d')
    const rows = (await readUnder(h, 3, 'bk/deep', ['path', 'depth']))!
    expect([...new Set(rows.map(r => r.path))]).toEqual(['bk/deep/x'])
    expect(await readUnder(h, 3, 'bk/deep', ['path'], 0)).toBe(null)
  })
})

describe('readAt', () => {
  it('reads a node’s own rows only', async () => {
    const h = await openIndex(env, DATE, 'age-pyramid-1d')
    const rows = (await readAt(h, 2, 'bk/new', ['path', 'depth', 'b']))!
    expect(rows.map(r => [r.path, r.depth, Number(r.b)])).toEqual([['bk/new', 2, 900], ['bk/new', 2, 600], ['bk/new', 2, 300]])
    expect(await readAt(h, 0, '', ['path'])).toHaveLength(18)
  })
})
