import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { AGE_EDGES_DAYS, bucketOf, childAges, scanDay, strata, withChildAges } from './childAges'
import { openIndex, readUnder } from './index'
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

/** The per-scan view's children of `path` and their `ag` (the `age_b*` columns). */
async function perScanAges(path: string): Promise<Map<string, number[]>> {
  const view = await buildView(env, { date: DATE, path, w: 4000, h: 4000, minArea: 1, atten: 1, threshold: 1, maxDepth: 1 })
  return new Map((view.tree.c ?? []).map(k => [k.n, k.ag as number[]]))
}

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

describe('childAges', () => {
  it('equals every child’s `age_b*` columns exactly', async () => {
    for (const path of ['', 'bk', 'bk/deep', 'bk/deep/x']) {
      const want = await perScanAges(path)
      expect({ path, ages: await childAges(env, DATE, path) }).toEqual({ path, ages: want })
    }
  })

  it('splits each edge on the right side (created hours within the UTC day, one future stamp)', async () => {
    expect((await childAges(env, DATE, 'bk'))!.get('mixed')).toEqual([
      1000 + 777, 1001 + 1006, 1007 + 1029, 1030 + 1090, 1091 + 1364, 1365 + 2094, 2095 + 5000,
    ])
  })

  it('is null for a scan without a pyramid', async () => {
    expect(await childAges(env, '2026-10-17T2207', 'bk')).toBe(null)
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

describe('withChildAges', () => {
  it('sets the bars of the children it has strata for, leaving the node and the rest', async () => {
    const tree: ViewNode = { n: 'bk', k: 'dir', b: 1, o: 1, c: [
      { n: 'mixed', k: 'dir', b: 1, o: 1 },
      { n: 'unknown', k: 'dir', b: 1, o: 1 },
    ] }
    const got = await withChildAges(env, DATE, 'bk', tree)
    expect([got.ag, got.c!.map(k => [k.n, k.ag])]).toEqual([undefined, [
      ['mixed', [1777, 2007, 2036, 2120, 2455, 3459, 7095]],
      ['unknown', undefined],
    ]])
  })
})
