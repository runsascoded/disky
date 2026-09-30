import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { serverTiming } from './edgeCache'
import { blobKey, columnsFor, groupMatchesSize, indexKey, type IndexHandle, openIndex, planRects, planSizeRects, readAsks, readRects, readSizeRects, type Rect, type Row, rowColumns, sizeVariant } from './index'
import { storeEnv } from './stores'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type DiffRow, SMALL_SUBTREE_ROWS, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The path store's reader (specs/path-store.md phase 2), over two real
// generations served from one D1 through the local-file store twin:
//
// - v1: `fixtures/path-index-zstd.parquet`, the dir-only index with the wire
//   names (`b, o, wts, wb, …`), scan 2026-09-01, the primary store.
// - v2: `fixtures/v2/`, a store generation cut by the current writer — every
//   row, objects included, the layer-2 names, no wire aliases, `path` and
//   `bysize` sorts in 2048-row groups (`fixtures/gen.py`): bucket `bk` holding
//   `flat/` (8000 objects cycling 16 log2 buckets: 500 at 32 KiB), `small/`
//   (1000, 2000, 3000 B), `nest/a/b/c0..3` (1 MiB each), `nest/a/z` (256 KiB)
//   and `empty.bin` (0 B). Served three ways: as the primary store with its
//   groups in D1 (scan 2026-09-30T0001), as the primary with its groups
//   retired to the `.groups.json` blob (2026-09-30T0002), and as the
//   secondary store `meta` (store-scoped rows, 2026-09-30T0001).

const V1 = '2026-09-01'
const V2 = '2026-09-30T0001'
const V2_BLOB = '2026-09-30T0002'
const V2_DIR = `cw-l2/${V2}/index/g2`
const KiB = 1024
const MiB = 1 << 20
const DAY = 20697 // 2026-09-01T00:00Z, epoch days: every fixture stamp

const META = { scope: 'admin', vars: { ROOT_LABEL: 'meta root', STORE_PREFIXES: 'meta-l2/' }, secrets: { STORE_ACCESS_KEY_ID: 'STORE_META_ACCESS_KEY_ID', STORE_SECRET_ACCESS_KEY: 'STORE_META_SECRET_ACCESS_KEY' } }
let env: Env
let meta: Env

beforeAll(async () => {
  // `openBlob` caches the manifest in the edge cache: a no-op one here.
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const v1 = await readJson<Record<string, D1Variant>>('path-index-zstd.d1.json')
  const v2 = await readJson<Record<string, D1Variant>>('v2/d1.json')
  const v2Files = { path: { parquet: 'v2/path-index.parquet', groups: 'v2/path-index.groups.json' }, bysize: { parquet: 'v2/path-index-bysize.parquet', groups: 'v2/path-index-bysize.groups.json' } }
  seedGeneration(raw, { date: V1, gen: 'g1', dir: `listing/${V1}/index/g1`, variants: v1, files: { path: { parquet: 'path-index-zstd.parquet', groups: 'path-index-zstd.groups.json' } } })
  seedGeneration(raw, { date: V2, gen: 'g2', dir: V2_DIR, variants: v2, files: v2Files })
  seedGeneration(raw, { date: V2_BLOB, gen: 'g3', dir: `cw-l2/${V2_BLOB}/index/g3`, variants: v2, files: v2Files, retired: ['path', 'bysize'] })
  seedGeneration(raw, { date: V2, gen: 'g2', dir: `meta-l2/${V2}/index/g2`, variants: v2, files: v2Files, store: 'meta' })
  env = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORES_JSON: JSON.stringify({ meta: META }), STORE_META_ACCESS_KEY_ID: 'mk', STORE_META_SECRET_ACCESS_KEY: 'ms' } as Env
  meta = storeEnv(env, 'meta', META)
})

/** The dir rows of the v2 fixture as `Row`s (objects have their own shape). */
const dir = (path: string, size: number, n_files: number, n_children: number, n_desc: number): Row =>
  ({ path, depth: path.split('/').length, usr: null, kind: 'dir', size, n_files, n_children, n_desc, mtime: 1788220800, mtime_mean: 1788220800, mtime_w: size, last_read: null, cls2: 0, cls3: 0, cls4: 0 })
const file = (path: string, size: number): Row =>
  ({ path, depth: path.split('/').length, usr: null, kind: 'file', size, n_files: 1, n_children: 0, n_desc: 1, mtime: 1788220800, mtime_mean: 1788220800, mtime_w: size, last_read: null, cls2: 0, cls3: 0, cls4: 0 })
const byPath = (rows: Row[]): Row[] => [...rows].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
/** `flat/`'s objects at bucket `2^e`: `f<i>` for `i ≡ e (mod 16)`. */
const flatAt = (e: number): string[] => Array.from({ length: 500 }, (_, i) => `bk/flat/f${String(16 * i + e).padStart(5, '0')}`)

describe('variant keys', () => {
  it('names the size sort and its lens twin beside the path sort', () => {
    expect(indexKey('d', 'bysize')).toBe('d/path-index-bysize.parquet')
    expect(indexKey('d', 'bysize-user')).toBe('d/path-index-bysize-by-user.parquet')
    expect(blobKey('d', 'bysize')).toBe('d/path-index-bysize.groups.json')
    expect([sizeVariant('path'), sizeVariant('user')]).toEqual(['bysize', 'bysize-user'])
  })
})

describe('generations', () => {
  it('opens each with its version, and projects a store read to the Row columns it has', async () => {
    const [h1, h2, hb, hs] = await Promise.all([openIndex(env, V1), openIndex(env, V2), openIndex(env, V2_BLOB), openIndex(env, V2, 'bysize')])
    expect([h1.mode, h1.version, h1.columns]).toEqual(['d1', 1, null])
    const v2cols = ['path', 'depth', 'kind', 'size', 'n_files', 'n_children', 'n_desc', 'mtime', 'mtime_mean', 'last_read']
    expect([h2.mode, h2.version, h2.columns]).toEqual(['d1', 2, v2cols])
    expect([hb.mode, hb.version, hb.columns]).toEqual(['blob', 2, v2cols])
    expect([hs.mode, hs.version, hs.variant]).toEqual(['d1', 2, 'bysize'])
    // A cw store has no `usr` and no class pivots; a gcs one would project them too.
    expect(rowColumns(2, [{ name: 'schema' }, { name: 'path' }, { name: 'usr' }, { name: 'size' }, { name: 'sum_storage_class_id_3' }, { name: 'created' }] as never)).toEqual(['path', 'usr', 'size', 'sum_storage_class_id_3'])
    expect(columnsFor(h2, ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4'])).toEqual(['path', 'depth', 'size', 'n_files'])
    expect(columnsFor(h1, ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4'])).toEqual(['path', 'depth', 'usr', 'b', 'o', 'c2', 'c3', 'c4'])
  })

  it('serves a v1 and a v2 generation side by side, and a store-scoped copy through its own env', async () => {
    const want = (r: Row) => r.depth <= 2
    const v1 = await readRects(await openIndex(env, V1), [{ dLo: 1, dHi: 2, pLo: '', pHi: '￿' }])
    expect(v1).toEqual([
      { path: 'bk', depth: 1, usr: null, kind: 'dir', size: 700, n_files: 3, n_children: null, n_desc: null, mtime: null, mtime_mean: 1788220800, mtime_w: 700, last_read: null, cls2: 0, cls3: 0, cls4: 0 },
      { path: 'bk/a', depth: 2, usr: null, kind: 'dir', size: 300, n_files: 2, n_children: null, n_desc: null, mtime: null, mtime_mean: 1788220800, mtime_w: 300, last_read: null, cls2: 0, cls3: 0, cls4: 0 },
      { path: 'bk/b', depth: 2, usr: null, kind: 'dir', size: 400, n_files: 1, n_children: null, n_desc: null, mtime: null, mtime_mean: 1788220800, mtime_w: 400, last_read: null, cls2: 0, cls3: 0, cls4: 0 },
    ])
    const top = [dir('bk', 37229948, 8009, 4, 8015), file('bk/empty.bin', 0), dir('bk/flat', 32767500, 8000, 8000, 8001), dir('bk/nest', 4456448, 5, 1, 8), dir('bk/small', 6000, 3, 3, 4)]
    for (const h of [await openIndex(env, V2), await openIndex(env, V2_BLOB), await openIndex(meta, V2)]) {
      expect((await readRects(h, [{ dLo: 1, dHi: 2, pLo: '', pHi: '￿' }])).filter(want)).toEqual(top)
    }
    expect((await openIndex(meta, V2)).file).not.toBe((await openIndex(env, V2)).file)
  })
})

interface Plan { tier: 'path' | 'bysize'; path: string; depth: number; thr: number; atten: number; max_depth: number | null; d_lo: number; d_hi: number | null; p_lo: string; p_hi: string; selected: number[] }

describe('span selection = `disk-tree tiers plan`', () => {
  // `fixtures/v2/plans.json`: the engine planner's group ids for each read,
  // on both sorts (`gen.py` PLANS). The reader's span queries — D1 SQL and
  // the blob handle's in-memory predicate — must select exactly those.
  it('bysize: b_max ≥ ⌊thr_min⌋ ∧ p_max ≥ P/ ∧ p_min < P0; path: the depth/path rect', async () => {
    const plans = await readJson<Plan[]>('v2/plans.json')
    expect(plans.map(p => [p.tier, p.path, p.thr, p.atten, p.max_depth, p.selected])).toEqual([
      ['path', '', 32768, 1, null, [0, 1, 2, 3]], ['bysize', '', 32768, 1, null, [0]],
      ['path', 'bk/flat', 32768, 1, null, [0, 1, 2, 3]], ['bysize', 'bk/flat', 32768, 1, null, [0]],
      ['path', 'bk/flat', 32768, 2, null, [0, 1, 2, 3]], ['bysize', 'bk/flat', 32768, 2, null, [0]],
      ['path', 'bk/small', 1, 1, null, [0, 3]], ['bysize', 'bk/small', 1, 1, null, [1]],
      ['path', 'bk/nest', MiB, 0.5, null, [0, 3]], ['bysize', 'bk/nest', MiB, 0.5, null, [0]],
      ['path', 'bk/nest', MiB, 0.5, 1, [0, 3]], ['bysize', 'bk/nest', MiB, 0.5, 1, [0]],
      ['path', 'bk/nest', MiB, 2, null, [0, 3]], ['bysize', 'bk/nest', MiB, 2, null, [0]],
    ])
    const got: unknown[] = []
    for (const date of [V2, V2_BLOB]) {
      for (const p of plans) {
        const h = await openIndex(env, date, p.tier)
        const rect: Rect = { dLo: p.d_lo, dHi: p.d_hi ?? 1e9, pLo: p.p_lo, pHi: p.p_hi }
        const thrAt = (d: number) => p.thr * p.atten ** Math.max(0, d - p.depth - 1)
        const spans = p.tier === 'path' ? await planRects(h, [rect], thrAt) : await planSizeRects(h, [rect], thrAt)
        got.push([h.mode, p.tier, p.path, p.thr, p.atten, p.max_depth, spans.map(s => s.rg)])
      }
    }
    expect(got).toEqual(['d1', 'blob'].flatMap(mode => plans.map(p => [mode, p.tier, p.path, p.thr, p.atten, p.max_depth, p.selected])))
  })

  it('groupMatchesSize: the floor is floored; the path range is half-open; a lens needs usr stats', () => {
    const g = { pMin: 'bk/flat/f00007', pMax: 'bk/small/s2', bMax: 3000 }
    expect(groupMatchesSize(g, [{ pLo: 'bk/small/', pHi: 'bk/small0' }], 3000.9)).toBe(true)
    expect(groupMatchesSize(g, [{ pLo: 'bk/small/', pHi: 'bk/small0' }], 3001)).toBe(false)
    expect(groupMatchesSize(g, [{ pLo: 'bk/small/s3', pHi: 'bk/small/s30' }])).toBe(false)
    expect(groupMatchesSize(g, [{ pLo: 'bk/small/s2', pHi: 'bk/small/s20' }])).toBe(true)
    expect(groupMatchesSize(g, [{ pLo: 'bk/z', pHi: 'bk/z0' }, { pLo: 'bk/flat/', pHi: 'bk/flat0' }])).toBe(true)
    expect(groupMatchesSize({ ...g, uMin: null, uMax: null }, [{ pLo: 'bk/flat/', pHi: 'bk/flat0' }], 0, { key: 'kim' })).toBe(false)
    expect(groupMatchesSize({ ...g, uMin: 'alice', uMax: 'zed' }, [{ pLo: 'bk/flat/', pHi: 'bk/flat0' }], 0, { key: 'kim' })).toBe(true)
  })
})

describe('reads', () => {
  it('bysize returns exactly the rows path returns above the per-depth threshold, from fewer groups', async () => {
    const path = await openIndex(env, V2)
    const size = await openIndex(env, V2, 'bysize')
    const rect: Rect = { dLo: 3, dHi: 1e9, pLo: 'bk/flat/', pHi: 'bk/flat0' }
    const thrAt = () => 32 * KiB
    const fromSize = await readSizeRects(size, [rect], thrAt)
    const fromPath = (await readRects(path, [rect], thrAt)).filter(r => r.size >= thrAt())
    expect(byPath(fromSize)).toEqual(byPath(fromPath))
    expect(fromSize).toEqual(flatAt(15).map(p => file(p, 32 * KiB)))
    expect([(await planSizeRects(size, [rect], thrAt)).length, (await planRects(path, [rect], thrAt)).length]).toEqual([1, 4])
  })

  it('attenuation is a per-row test on bysize: deeper rows need more (atten 2) or less (atten 0.5)', async () => {
    const size = await openIndex(env, V2, 'bysize')
    const rect: Rect = { dLo: 3, dHi: 1e9, pLo: 'bk/nest/', pHi: 'bk/nest0' }
    // dP = 2: depth 3 → thr, 4 → 2·thr, 5 → 4·thr
    const up = await readSizeRects(size, [rect], d => 256 * KiB * 2 ** Math.max(0, d - 3))
    expect(up.map(r => [r.path, r.kind, r.size])).toEqual([
      ['bk/nest/a', 'dir', 4456448], ['bk/nest/a/b', 'dir', 4 * MiB],
      ['bk/nest/a/b/c0', 'file', MiB], ['bk/nest/a/b/c1', 'file', MiB], ['bk/nest/a/b/c2', 'file', MiB], ['bk/nest/a/b/c3', 'file', MiB],
    ])
    // depth 3 → 1 MiB, 4 → 512 KiB, 5 → 256 KiB: `z` (256 KiB at depth 4) still folds
    const down = await readSizeRects(size, [rect], d => MiB * 0.5 ** Math.max(0, d - 3))
    expect(down.map(r => r.path)).toEqual(up.map(r => r.path))
    // capped one level down: `nest/a` alone
    expect((await readSizeRects(size, [{ ...rect, dHi: 3 }], () => MiB)).map(r => r.path)).toEqual(['bk/nest/a'])
  })

  it('point lookups read path: an object row and a dir row, from the groups that may hold them', async () => {
    const h = await openIndex(env, V2)
    const want = new Set(['bk/flat', 'bk/small/s1'])
    const got = await readAsks(h, [{ depth: 2, path: 'bk/flat' }, { depth: 3, path: 'bk/small/s1' }], r => want.has(r.path))
    expect(got).toEqual({ rows: [dir('bk/flat', 32767500, 8000, 8000, 8001), file('bk/small/s1', 2000)], groups: 2 })
  })
})

describe('buildView on a store generation', () => {
  const base = { w: 1280, h: 800, minArea: 12, atten: 1 }
  const node = (n: string, k: 'file' | 'dir', b: number, o: number, rest: Partial<ViewNode> = {}): ViewNode => ({ n, k, b, o, d: DAY, ...rest })
  const flatKids = (): ViewNode[] => [...flatAt(15).map(p => node(p.split('/').pop()!, 'file', 32 * KiB, 1)), node('(other)', 'dir', 32767500 - 500 * 32 * KiB, 7500, { f: 7500 })]
  const nestTree = (): ViewNode => node('nest', 'dir', 4456448, 5, { c: [
    node('a', 'dir', 4456448, 5, { c: [
      node('b', 'dir', 4 * MiB, 4, { c: ['c0', 'c1', 'c2', 'c3'].map(n => node(n, 'file', MiB, 1)) }),
      node('z', 'file', 256 * KiB, 1),
    ] }),
  ] })

  it('the root, from bysize: objects and dirs with `k`, `(other)` = P − Σ kept with f = n_children − kept', async () => {
    const v = await buildView(env, { ...base, date: V2, path: '', threshold: 32 * KiB, smallRows: 4096 })
    expect([v.tier, v.index, v.threshold, v.nodes, v.truncated]).toEqual(['bysize', 'd1', 32 * KiB, 510, false])
    // `small` (6000) and `empty.bin` fold under `bk`, but the fold's 6000 B is under the threshold: no `(other)` cell there.
    expect(v.tree).toEqual(node('root', 'dir', 37229948, 8009, { c: [
      node('bk', 'dir', 37229948, 8009, { c: [
        node('flat', 'dir', 32767500, 8000, { c: flatKids() }),
        nestTree(),
      ] }),
    ] }))
  })

  it('the same view from path (a small subtree by the default cutoff), the blob-served copy, and the secondary store', async () => {
    const want = await buildView(env, { ...base, date: V2, path: '', threshold: 32 * KiB, smallRows: 4096 })
    for (const [e, date, tier, index] of [[env, V2, 'path', 'd1'], [env, V2_BLOB, 'bysize', 'blob'], [meta, V2, 'bysize', 'd1']] as const) {
      const v = await buildView(e, { ...base, date, path: '', threshold: 32 * KiB, ...(tier === 'path' ? {} : { smallRows: 4096 }) })
      expect([v.tier, v.index, v.nodes]).toEqual([tier, index, 510])
      expect(v.tree).toEqual({ ...want.tree, n: e === meta ? 'meta root' : 'root' })
    }
    expect(SMALL_SUBTREE_ROWS).toBe(24576)
  })

  it('a flat directory: its objects are the children; a tiny one reads path whole', async () => {
    const flat = await buildView(env, { ...base, date: V2, path: 'bk/flat', threshold: 32 * KiB, smallRows: 0 })
    expect([flat.tier, flat.nodes]).toEqual(['bysize', 500])
    expect(flat.tree).toEqual(node('flat', 'dir', 32767500, 8000, { c: flatKids() }))
    const small = await buildView(env, { ...base, date: V2, path: 'bk/small', threshold: 1 })
    expect([small.tier, small.nodes]).toEqual(['path', 3])
    expect(small.tree).toEqual(node('small', 'dir', 6000, 3, { c: [node('s2', 'file', 3000, 1), node('s1', 'file', 2000, 1), node('s0', 'file', 1000, 1)] }))
  })

  it('attenuated (atten 2): a deeper row needs more bytes; a folded object leaves no `(other)` under the cell budget', async () => {
    const v = await buildView(env, { ...base, atten: 2, date: V2, path: 'bk/nest', threshold: 256 * KiB, smallRows: 0 })
    expect([v.tier, v.nodes]).toEqual(['bysize', 6])
    expect(v.tree).toEqual(node('nest', 'dir', 4456448, 5, { c: [
      node('a', 'dir', 4456448, 5, { c: [node('b', 'dir', 4 * MiB, 4, { c: ['c0', 'c1', 'c2', 'c3'].map(n => node(n, 'file', MiB, 1)) })] }),
    ] }))
  })

  it('a depth cap: rows at the cap come back childless, from the band read alone', async () => {
    const v = await buildView(env, { ...base, date: V2, path: '', threshold: 32 * KiB, maxDepth: 2, smallRows: 0 })
    expect([v.tier, v.nodes]).toEqual(['bysize', 3])
    expect(v.tree).toEqual(node('root', 'dir', 37229948, 8009, { c: [
      node('bk', 'dir', 37229948, 8009, { c: [node('flat', 'dir', 32767500, 8000), node('nest', 'dir', 4456448, 5)] }),
    ] }))
  })

  it('a v1 generation is unchanged: dirs only, every node `dir`, the fine tier', async () => {
    const v = await buildView(env, { ...base, date: V1, path: '', threshold: 1 })
    expect([v.tier, v.index, v.nodes]).toEqual(['fine', 'd1', 3])
    expect(v.tree).toEqual(node('root', 'dir', 700, 3, { c: [node('bk', 'dir', 700, 3, { c: [node('b', 'dir', 400, 1), node('a', 'dir', 300, 2)] })] }))
  })

  it('Server-Timing names the sort that answered', async () => {
    const st = serverTiming()
    await buildView(env, { ...base, date: V2, path: 'bk/flat', threshold: 32 * KiB, smallRows: 0, trace: st.trace })
    // (`group`, the per-decode phase, is absent when the isolate's group cache answers.)
    const descs = (h: string) => Object.fromEntries([...h.matchAll(/(groups|rows);dur=\d+;desc="([^"]*)"/g)].map(m => [m[1], m[2]]))
    expect(descs(st.header())).toEqual({ groups: 'path+bysize', rows: 'bysize' })
    const st2 = serverTiming()
    await buildView(env, { ...base, date: V2, path: 'bk/small', threshold: 1, trace: st2.trace })
    expect(descs(st2.header())).toEqual({ groups: 'path', rows: 'path' })
  })
})

describe('buildDiff', () => {
  const base = { w: 1280, h: 800, minArea: 12, atten: 1, top: 500 }
  const row = (p: string, d: number, k: DiffRow['k'], s: DiffRow['s'], a: number, b: number, oa: number, ob: number, x?: true): DiffRow => ({ p, d, k, s, a, b, oa, ob, ...(x ? { x } : {}) })

  it('objects are real rows: added under a path the older scan lacks, removed the other way', async () => {
    const added = await buildDiff(env, { ...base, from: V1, to: V2, path: 'bk/nest', threshold: 1 })
    expect(added).toEqual({
      rows: [
        row('a', 1, 'dir', 'added', 0, 4456448, 0, 5, true),
        row('a/b', 2, 'dir', 'added', 0, 4 * MiB, 0, 4, true),
        row('a/b/c0', 3, 'file', 'added', 0, MiB, 0, 1),
        row('a/b/c1', 3, 'file', 'added', 0, MiB, 0, 1),
        row('a/b/c2', 3, 'file', 'added', 0, MiB, 0, 1),
        row('a/b/c3', 3, 'file', 'added', 0, MiB, 0, 1),
        row('a/z', 2, 'file', 'added', 0, 256 * KiB, 0, 1),
      ],
      total_a: 0, total_b: 4456448, objects_a: 0, objects_b: 5, threshold: 1, tier: 'path',
      expansions: 3, truncated: false, lookups: 0, lookups_capped: false,
    })
    const removed = await buildDiff(env, { ...base, from: V2, to: V2_BLOB, path: 'bk/small', threshold: 1 })
    expect(removed.rows).toEqual([])
    expect([removed.total_a, removed.total_b, removed.tier]).toEqual([6000, 6000, 'path'])
    const gone = await buildDiff(env, { ...base, from: V2, to: V1, path: 'bk/small', threshold: 1 })
    expect(gone.rows).toEqual([
      row('s2', 1, 'file', 'removed', 3000, 0, 1, 0),
      row('s1', 1, 'file', 'removed', 2000, 0, 1, 0),
      row('s0', 1, 'file', 'removed', 1000, 0, 1, 0),
    ])
  })

  it('a v1 side against a store side compares directories only: the store\'s objects fold into `(other)`', async () => {
    const d = await buildDiff(env, { ...base, from: V1, to: V2, path: '', threshold: 1 })
    expect(d).toEqual({
      rows: [
        row('bk', 1, 'dir', 'changed', 700, 37229948, 3, 8009, true),
        row('bk/nest', 2, 'dir', 'added', 0, 4456448, 0, 5, true),
        row('bk/nest/a', 3, 'dir', 'added', 0, 4456448, 0, 5, true),
        row('bk/flat', 2, 'dir', 'added', 0, 32767500, 0, 8000),
        row('bk/nest/a/b', 4, 'dir', 'added', 0, 4 * MiB, 0, 4),
        row('bk/nest/a/(other)', 4, 'dir', 'added', 0, 256 * KiB, 0, 1),
        row('bk/small', 2, 'dir', 'added', 0, 6000, 0, 3),
        row('bk/b', 2, 'dir', 'removed', 400, 0, 1, 0),
        row('bk/a', 2, 'dir', 'removed', 300, 0, 2, 0),
      ],
      total_a: 700, total_b: 37229948, objects_a: 3, objects_b: 8009, threshold: 1, tier: 'path',
      expansions: 4, truncated: false, lookups: 7, lookups_capped: false,
    })
  })
})

describe('serverTiming desc', () => {
  it('collects each phase\'s distinct labels in first-seen order', () => {
    const st = serverTiming()
    st.trace('rows', 5, 'bysize')
    st.trace('rows', 3)
    st.trace('groups', 1, 'bysize')
    st.trace('groups', 2, 'path')
    st.trace('groups', 2, 'bysize')
    st.trace('spans', 4)
    expect(st.header().replace(/total;dur=\d+/, 'total;dur=<t>')).toBe('rows;dur=8;desc="bysize", groups;dur=5;desc="bysize+path", spans;dur=4, total;dur=<t>')
  })
})

// The fixture files every test above reads exist (a stale `gen.py` run would
// otherwise fail far from here).
it('fixtures are registered', () => {
  expect([...FILES.keys()].sort()).toEqual([
    `${V2_DIR}/path-index-bysize.groups.json`, `${V2_DIR}/path-index-bysize.parquet`, `${V2_DIR}/path-index.groups.json`, `${V2_DIR}/path-index.parquet`,
    `cw-l2/${V2_BLOB}/index/g3/path-index-bysize.groups.json`, `cw-l2/${V2_BLOB}/index/g3/path-index-bysize.parquet`,
    `cw-l2/${V2_BLOB}/index/g3/path-index.groups.json`, `cw-l2/${V2_BLOB}/index/g3/path-index.parquet`,
    `listing/${V1}/index/g1/path-index.groups.json`, `listing/${V1}/index/g1/path-index.parquet`,
    `meta-l2/${V2}/index/g2/path-index-bysize.groups.json`, `meta-l2/${V2}/index/g2/path-index-bysize.parquet`, `meta-l2/${V2}/index/g2/path-index.groups.json`, `meta-l2/${V2}/index/g2/path-index.parquet`,
  ])
  expect(fixture('v2/d1.json').endsWith('/functions/_lib/fixtures/v2/d1.json')).toBe(true)
})
