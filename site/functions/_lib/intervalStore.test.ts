import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parquetMetadata, parquetReadObjects } from 'hyparquet'
import { asOfScans, type Ask, groupDoc, groupMatchesSize, groupRows, IV_BROKEN_TTL, ivRetry, IvGroup, openIndex, pathGens, planRects, planSizeRects, readAsks, readRows, readSizeRects, type Rect, type Row, sizeBucket, sizeNeighbours, type Span, usMap, withPathStore } from './index'
import { sqliteD1 } from './testD1'
import { compressors } from './zstd'
import { type D1Variant, fixture, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The change-interval path store read as of a scan (`PATH_STORE=intervals`, specs/interval-store.md):
// `fixtures/iv/` is one generation over six synthetic scans (three v1 indexes, three v2 store
// generations; owner slices, paths that come and go, moving read days), 4 rows per group, built by
// `dt_cloud.interval_store` (`fixtures/iv/gen.py`). `expected.json` holds each view computed by the
// Python per-scan reference from that scan's own rows; the Worker's view over the store must equal it.

type Tiles = Record<string, Record<string, unknown>>
type Case = { date: string; path: string; w: number; h: number; depth: number | null; v: number; tiles: Tiles }

/** `INDEX_R2` over the fixture dir, counting what is read; `hide(key)`: an object that isn't there (a run's file not
 *  copied yet), to `get` and `list`, and with `head: false` the binding has no `head` (so only a read finds it). */
function r2(reads: string[], hide: (key: string) => boolean = () => false, { head = true }: { head?: boolean } = {}): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const nodeFs = async () => {
    const mod = 'node:fs' // a variable: the Workers typecheck has no Node types
    return (await import(/* @vite-ignore */ mod)) as Fs
  }
  const there = async (key: string) => !hide(key) && (await nodeFs()).existsSync(fixture(`iv/${key}`))
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const fs = await nodeFs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${dir}${f}`).filter(k => k.startsWith(prefix) && !hide(k) && fs.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const fs = await nodeFs()
    const file = fixture(`iv/${key}`)
    if (!await there(key)) return null
    const buf = fs.readFileSync(file)
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    reads.push(key)
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  return { get, list, ...(head ? { head: async (key: string) => (await there(key) ? { key } : null) } : {}) } as unknown as R2Bucket
}

/** A node without its children as canonical JSON (`interval_read.canon`: sorted keys, compact). */
function canon(n: ViewNode): string {
  const sorted = (v: unknown): unknown =>
    Array.isArray(v) ? v.map(sorted) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sorted((v as Record<string, unknown>)[k])])) : v
  const { c: _c, ...rest } = n
  return JSON.stringify(sorted(rest))
}

/** A tree flattened as `interval_read.flatten`: keyed by names from the root, siblings by bytes then
 *  name, later duplicates `#2`…; owners by bytes then name (the Worker keeps row order on ties). */
function flatten(t: ViewNode): Tiles {
  const out: Tiles = {}
  const rec = (n: ViewNode, p: string) => {
    let k = p
    for (let i = 2; k in out; i++) k = `${p}#${i}`
    const { c, ...rest } = n
    const us = rest.us as [string, number][] | undefined
    out[k] = { ...rest, ...(us ? { us: [...us].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)) } : {}) }
    const kids = [...(c ?? [])].sort((a, b) => b.b - a.b || (a.n < b.n ? -1 : a.n > b.n ? 1 : 0) || (canon(a) < canon(b) ? -1 : canon(a) > canon(b) ? 1 : 0))
    for (const k2 of kids) rec(k2, `${p}/${k2.n}`)
  }
  rec(t, '')
  return out
}

let env: Env
let reads: string[]
let cases: Case[]
/** How the views of `expected.json` are served when every subtree is big (`ivPlanRows: 0`): view kind → sort. */
const TIERS_AT_PLAN_ROWS_0: Record<string, number> = { 'full bysize': 42, 'band path': 42 }

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1('cw')
  reads = []
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: r2(reads) } as Env
  cases = await readJson<Case[]>('iv/expected.json')
})

describe('interval store', () => {
  it('opens a held date as of its scan, and nothing else', async () => {
    const h = await openIndex(env, '2026-08-03', 'bysize')
    expect([h.mode, h.version, h.variant, h.asOf, h.gen]).toEqual(['pq', 3, 'bysize', 1785715200, 'iv:g1'])
    await expect(openIndex(env, '2026-08-01', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-01")
    await expect(openIndex(env, '2026-08-03', 'bysize-user')).rejects.toThrow("index variant 'bysize-user' not synced for 2026-08-03")
  })

  it('opts a request in with `ps=iv` under `PATH_STORE=opt-in`, never for a secondary store', () => {
    const at = (q: string, mode = 'opt-in') => withPathStore({ request: new Request(`https://x/api/subtree?${q}`), env: { ...env, PATH_STORE: mode } }).env.PATH_STORE
    expect([at('date=d'), at('date=d&ps=iv'), at('date=d&ps=iv&store=meta'), at('date=d', 'intervals'), at('date=d&store=meta', 'intervals')])
      .toEqual([undefined, 'intervals', undefined, 'intervals', undefined])
  })

  it('keys a held date\'s answers to the interval generation', async () => {
    const off = { ...env, PATH_STORE: undefined }
    const [held, heldOff, other, otherOff] = await Promise.all([pathGens(env, ['2026-08-03']), pathGens(off, ['2026-08-03']), pathGens(env, ['2026-08-01']), pathGens(off, ['2026-08-01'])])
    expect([held === heldOff, other === otherOff]).toEqual([false, true])
  })

  it('keys a rewritten generation apart by its revision, reading the same objects', async () => {
    const rev = { ...env, INTERVAL_STORE_REV: '2' }
    const [h, g, g0] = await Promise.all([openIndex(rev, '2026-08-03', 'path'), pathGens(rev, ['2026-08-03']), pathGens(env, ['2026-08-03'])])
    const c = cases.find(x => x.date === '2026-08-03' && x.path === 'b2/e' && x.w === 8 && x.depth == null)!
    const v = await buildView(rev, { date: c.date, path: c.path, w: 8, h: 6, minArea: 12, atten: 2 })
    expect([h.gen, g === g0, flatten(v.tree)['']]).toEqual(['iv:g1@2', false, c.tiles['']])
  })

  it('holds a decoded group as columns, exactly, or not at all', () => {
    const raw = [{ depth: 2n, path: 'b1/x', size: 5, us: null, mtime: undefined, wts: 1.25 }, { depth: 3n, path: 'b1/\ud800y', size: 0, us: 'alice', mtime: 7n, wts: -0.5 }]
    const cols = ['depth', 'path', 'size', 'us', 'mtime', 'wts']
    const d = groupDoc(raw, cols)
    expect(d).toEqual({ cols, v: [[2, 3], ['b1/x', 'b1/\ud800y'], [5, 0], [null, 'alice'], [null, 7], [1.25, -0.5]] })
    expect(groupRows(JSON.parse(JSON.stringify(d)))).toEqual([
      { depth: 2, path: 'b1/x', size: 5, us: null, mtime: null, wts: 1.25 },
      { depth: 3, path: 'b1/\ud800y', size: 0, us: 'alice', mtime: 7, wts: -0.5 },
    ])
    expect([groupDoc([{ a: 2n ** 53n }], ['a']), groupDoc([{ a: NaN }], ['a']), groupDoc([{ a: new Uint8Array(1) }], ['a']), groupDoc([{ a: 2n ** 53n - 1n }], ['a'])])
      .toEqual([null, null, null, { cols: ['a'], v: [[2 ** 53 - 1]] }])
  })

  it('decodes owner slices', () => {
    expect([usMap('', 5), usMap('alice', 5), usMap('[["alice",0],["bob",10]]', 10)]).toEqual([null, { alice: 5 }, { alice: 0, bob: 10 }])
  })

  it('serves every view of every scan as the per-scan reference draws it', async () => {
    expect(cases.length).toBe(84)
    for (const c of cases) {
      const v = await buildView(env, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
      const got = flatten(v.tree)
      // A v1 scan never knew its directories' children: the reader's `(other)` count is what its read saw.
      const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
      if (c.v === 1) for (const n of Object.values(got)) delete n.f
      expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
      expect([v.index, ['bysize', 'path'].includes(v.tier)]).toEqual(['iv:g1', true])
    }
  })

  it('reads a big subtree\'s depth band from `path`, its whole subtree from `bysize`: every view as the reference draws it', async () => {
    // `ivPlanRows: 0` makes every subtree big (gcs's buckets): a full view plans `bysize` alone, a depth band (`depth=N`)
    // plans `path` first and reads it while it holds at most `IV_BAND_ROWS`.
    const tiers: Record<string, number> = {}
    for (const c of cases) {
      const v = await buildView(env, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ivPlanRows: 0, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
      const got = flatten(v.tree)
      const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
      if (c.v === 1) for (const n of Object.values(got)) delete n.f
      expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
      const k = `${c.depth == null ? 'full' : 'band'} ${v.tier}`
      tiers[k] = (tiers[k] ?? 0) + 1
    }
    expect(tiers).toEqual(TIERS_AT_PLAN_ROWS_0)
  })

  for (const gen of ['g1', 'g3']) {
    it(`serves a new isolate from the colo: state, footers, footer groups and row groups decoded once per colo, every view and diff the same (${gen})`, async () => {
      // Two isolates (fresh module graphs) over one colo cache: the second reads nothing from R2.
      const dates = [...new Set(cases.map(c => c.date))].sort()
      const pairs = dates.slice(1).map((d, i) => [dates[i], d])
      const diffOf = async (b: typeof buildDiff, e: Env, [from, to]: string[]) => {
        const d = await b(e, { from, to, path: '', w: 8, h: 6, minArea: 12, atten: 2, top: 100 })
        return { rows: d.rows, totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
      }
      const diffs = await Promise.all(pairs.map(p => diffOf(buildDiff, env, p)))
      const m = new Map<string, Response>()
      const colo = { match: async (r: Request) => m.get(r.url)?.clone(), put: async (r: Request, v: Response) => { m.set(r.url, v.clone()) } }
      const was = (globalThis as unknown as { caches: unknown }).caches
      ;(globalThis as unknown as { caches: unknown }).caches = { default: colo }
      const views = cases.filter(x => x.w === 30)
      try {
        const isolate = async () => {
          vi.resetModules()
          const view = await import('./view')
          const got: string[] = []
          const e = { ...env, INTERVAL_STORE_GEN: gen, INTERVAL_STORE_REV: 'colo', INDEX_R2: r2(got) } as Env
          const trees = []
          for (const c of views) {
            const t = flatten((await view.buildView(e, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })).tree)
            if (c.v === 1) for (const n of Object.values(t)) delete n.f
            trees.push(t)
          }
          const ds = []
          for (const p of pairs) ds.push(await diffOf(view.buildDiff, e, p))
          await (await import('./index')).coloPutsSettled()
          return { trees, ds, files: [...new Set(got)].map(k => k.replace(`interval-store/${gen}/`, '')).sort() }
        }
        const a = await isolate()
        const b = await isolate()
        expect(a.trees).toEqual(views.map(c => Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))))
        expect(b.trees).toEqual(a.trees)
        expect([a.ds, b.ds]).toEqual([diffs, diffs])
        // g3's 08-05 run's `bysize` data is read by nothing here: only 08-05's reads open that run, and they plan `path`
        // (a small subtree reads the sort holding fewer rows).
        const data = (d: string) => [...(d === 'deltas/2026-08-05/' ? [] : [`${d}served/bysize.parquet`]), `${d}served/path.parquet`]
        const dirs = ['', ...(gen === 'g3' ? ['deltas/2026-08-04/', 'deltas/2026-08-05/'] : [])]
        expect([a.files, b.files]).toEqual([
          [...(gen === 'g3' ? ['manifests/2026-08-05.json'] : []), 'scans.json', ...dirs.flatMap(d => [`${d}served/bysize.groups.parquet`, `${d}served/path.groups.parquet`, ...data(d)])].sort(),
          [],
        ])
      } finally {
        ;(globalThis as unknown as { caches: unknown }).caches = was
      }
    })
  }

  it('reads only the interval store, read days from the path versions themselves', async () => {
    expect([...new Set(reads)].sort()).toEqual([
      'interval-store/g1/scans.json',
      'interval-store/g1/served/bysize.groups.parquet',
      'interval-store/g1/served/bysize.parquet',
      'interval-store/g1/served/path.groups.parquet',
      'interval-store/g1/served/path.parquet',
    ])
  })

  it('a lookup bounded by another scan\'s version of the path finds exactly what an unbounded one does', async () => {
    // What a diff knows of a name it looks up (`Ask.vtLe` / `vfGe`): the other scan's row. A path's
    // versions never overlap, so the one live here closed by the time that row's opened, or opened once
    // it closed. Every row live at one scan, asked at every other where it isn't live. (This store's
    // segments each hold the versions closing at one scan, so a scan's own time test already selects
    // only groups that can answer and the bound prunes nothing more; on dyadic segments it skips the
    // open segment and the blocks outside the window — measured on gcs, specs/interval-store.md §6.2.)
    const DATES = [...new Set(cases.map(c => c.date))].sort()
    const live = new Map(await Promise.all(DATES.map(async d => [d, await readRows(await openIndex(env, d, 'path'), 1, 64, '', '\uffff')] as const)))
    const key = (r: { depth: number; path: string }) => `${r.depth}\0${r.path}`
    const rowsOf = (rs: Row[]) => rs.map(r => [r.depth, r.path, r.vf, r.vt, r.size, r.n_files]).sort((x, y) => (`${x[0]}${x[1]}${x[2]}` < `${y[0]}${y[1]}${y[2]}` ? -1 : 1))
    let asked = 0
    let found = 0
    const groups = { bounded: 0, plain: 0 }
    for (const here of DATES) {
      const h = await openIndex(env, here, 'path')
      const D = h.asOf!
      for (const there of DATES) {
        if (there === here) continue
        const asks: Ask[] = live.get(there)!.filter(r => !(r.vf! <= D && D < r.vt!))
          .map(r => ({ depth: r.depth, path: r.path, ...(D < r.vf! ? { vtLe: r.vf! } : { vfGe: r.vt! }) }))
        const want = new Set(asks.map(a => key(a as { depth: number; path: string })))
        const keep = (r: Row) => want.has(key(r))
        const [b, u] = await Promise.all([
          readAsks(h, asks, keep, { maxGroups: 1 << 20 }),
          readAsks(h, asks.map(a => ({ depth: a.depth, path: (a as { path: string }).path })), keep, { maxGroups: 1 << 20 }),
        ])
        expect({ here, there, rows: rowsOf(b.rows) }).toEqual({ here, there, rows: rowsOf(u.rows) })
        asked += asks.length
        found += b.rows.length
        groups.bounded += b.groups
        groups.plain += u.groups
      }
    }
    expect({ asked, found, groups }).toEqual({ asked: 580, found: 510, groups: { bounded: 155, plain: 155 } })
  })

  it('leaves a per-scan read of a date it holds as it was: one isolate serves both under `PATH_STORE=opt-in`', async () => {
    // A per-scan v1 generation of the store's first scan, with a coarse tier (the fine tier's rows, floor 1).
    const D = '2026-07-30'
    const { db, raw } = await sqliteD1('cw')
    const v1 = await readJson<Record<string, D1Variant>>('path-index-zstd.d1.json')
    const tier = { parquet: 'path-index-zstd.parquet', groups: 'path-index-zstd.groups.json' }
    seedGeneration(raw, { date: D, gen: 'p1', dir: `listing/${D}/index/p1`, variants: { ...v1, coarse20: { ...v1.path, schema: { ...v1.path.schema, floor_bytes: 1 } } }, files: { path: tier, coarse20: tier } })
    const per = { DB: db, ROOT_LABEL: 'all buckets', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
    const o = { date: D, path: '', w: 1280, h: 800, minArea: 12, atten: 2, threshold: 1 }
    const before = await buildView(per, o)
    // The store's read of the date finds its `path` sort a store sort, so no coarse tier answers it…
    const held = await buildView({ ...per, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: r2([]) } as Env, o)
    // …which says nothing about the per-scan generation's coarse tier.
    const after = await buildView(per, o)
    expect([before.index, before.tier, held.index, after.index, after.tier, after.tree]).toEqual(['d1', 'coarse20', 'iv:g1', 'd1', 'coarse20', before.tree])
  })

  it('diffs two scans from the store', async () => {
    const d = await buildDiff(env, { from: '2026-08-04', to: '2026-08-05', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, top: 100 })
    const a = cases.find(c => c.date === '2026-08-04' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    const b = cases.find(c => c.date === '2026-08-05' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    expect([d.total_a, d.total_b]).toEqual([a.tiles[''].b, b.tiles[''].b])
  })
})

// Base + per-scan runs (specs/interval-store.md §2.7, `interval_append`): `g3`, `g4` and `g6` hold `g1`'s six scans as a
// base of four and a run per later scan — unmerged (two tiers), merged into one by the old inline pairwise carry, and
// merged by the deferred carries (`append_runner`: each scan's manifest lists its level-0 run, a merge's revision
// `2026-08-05.m001.json` the merged run) — listed by the newest manifest (`fixtures/iv/gen.py tiered`). Every view must be
// `g1`'s, and a broken run is cut out, never a failed request.
describe('interval store: base + runs', () => {
  const RUN_DATES = ['2026-08-04', '2026-08-05']
  // `rev`: a test's own isolate caches (state, footers, groups and broken runs are keyed by the generation's revision).
  const at = (gen: string, reads: string[] = [], hide?: (k: string) => boolean, o?: { head?: boolean; rev?: string }): Env =>
    ({ ...env, INTERVAL_STORE_GEN: gen, INDEX_R2: r2(reads, hide, o), ...(o?.rev ? { INTERVAL_STORE_REV: o.rev } : {}) }) as Env

  it('opens every scan with every run beside the base', async () => {
    const shape = async (gen: string, date: string) => {
      const h = await openIndex(at(gen), date, 'path')
      return [h.asOf, h.gen, (h.runs ?? []).map(r => [r.run, r.gen, r.asOf])]
    }
    expect(await Promise.all([shape('g3', '2026-08-03'), shape('g3', '2026-08-05'), shape('g4', '2026-08-04'), shape('g6', '2026-08-05')])).toEqual([
      [1785715200, 'iv:g3', [['deltas/2026-08-04', 'iv:g3/deltas/2026-08-04', 1785715200], ['deltas/2026-08-05', 'iv:g3/deltas/2026-08-05', 1785715200]]],
      [1785888000, 'iv:g3', [['deltas/2026-08-04', 'iv:g3/deltas/2026-08-04', 1785888000], ['deltas/2026-08-05', 'iv:g3/deltas/2026-08-05', 1785888000]]],
      [1785801600, 'iv:g4', [['deltas/2026-08-04_2026-08-05', 'iv:g4/deltas/2026-08-04_2026-08-05', 1785801600]]],
      // the revision `2026-08-05.m001.json`, over its scan's own `2026-08-05.json`
      [1785888000, 'iv:g6', [['deltas/2026-08-04_2026-08-05', 'iv:g6/deltas/2026-08-04_2026-08-05', 1785888000]]],
    ])
  })

  it('takes a merge\'s revision over its scan\'s manifest, and reads the scan\'s own before the revision lands (deferred carries)', async () => {
    const runs = async (e: Env) => ((await openIndex(e, '2026-08-05', 'path')).runs ?? []).map(r => r.run)
    const reads: string[] = []
    expect(await runs(at('g6', reads, undefined, { rev: 'rev-on' }))).toEqual(['deltas/2026-08-04_2026-08-05'])
    expect(reads.filter(k => k.includes('/manifests/'))).toEqual(['interval-store/g6/manifests/2026-08-05.m001.json'])
    const before = (k: string) => k.endsWith('/manifests/2026-08-05.m001.json')
    expect(await runs(at('g6', [], before, { rev: 'rev-off' }))).toEqual(['deltas/2026-08-04', 'deltas/2026-08-05'])
    // …and every view of every scan is g1's either way (the revision is the same scans, an equal stack).
    for (const [rev, hide] of [['rev-views-on', undefined], ['rev-views-off', before]] as const) {
      for (const c of cases.filter(x => x.w === 30 && x.depth == null)) {
        const v = await buildView(at('g6', [], hide, { rev }), { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2 })
        const got = flatten(v.tree)
        if (c.v === 1) for (const n of Object.values(got)) delete n.f
        const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
        expect({ rev, case: [c.date, c.path], tiles: got }).toEqual({ rev, case: [c.date, c.path], tiles: want })
      }
    }
  })

  for (const gen of ['g3', 'g4', 'g6']) {
    it(`serves every view of every scan as g1 does (${gen}: runs ${{ g3: 'unmerged', g4: 'merged pairwise', g6: 'merged, a revision' }[gen]})`, async () => {
      const e = at(gen)
      for (const c of cases) {
        const v = await buildView(e, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
        const got = flatten(v.tree)
        const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
        if (c.v === 1) for (const n of Object.values(got)) delete n.f
        expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
        expect(v.index).toBe(`iv:${gen}`)
      }
    })

    it(`diffs every pair of scans as g1 does (${gen})`, async () => {
      const dates = [...new Set(cases.map(c => c.date))].sort()
      for (const from of dates) {
        for (const to of dates) {
          if (from === to) continue
          for (const path of ['', 'b1', 'b2/e']) {
            const o = { from, to, path, w: 8, h: 6, minArea: 12, atten: 2, top: 100 }
            const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d : { rows: d.rows, totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
            const got = await buildDiff(at(gen), o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
            const want = await buildDiff(env, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
            expect({ o, d: shape(got) }).toEqual({ o, d: shape(want) })
          }
        }
      }
    })
  }

  it('plans from footer groups decoded exactly: every tier group\'s bounds, time bounds and rows, as its `.groups.parquet` holds them', async () => {
    // g4's merged run is the fixture whose groups' `vf` / `vt` ranges are wider than a point.
    const nodeFs = async () => (await import(/* @vite-ignore */ 'node:fs' as string)) as { readFileSync(p: string): Uint8Array }
    const truth = async (dir: string) => {
      const b = (await nodeFs()).readFileSync(fixture(`iv/interval-store/g4/${dir}served/path.groups.parquet`))
      const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
      const rows = await parquetReadObjects({ file: ab, metadata: parquetMetadata(ab), compressors }) as Record<string, unknown>[]
      const n = (v: unknown) => Number(v)
      return new Map(rows.map(r => [n(r.rg), [n(r.d_min), n(r.d_max), r.p_min, r.p_max, n(r.b_max), n(r.vf_min), n(r.vf_max), n(r.vt_min), n(r.vt_max), n(r.row_start), n(r.row_end)]]))
    }
    const want = [await truth(''), await truth('deltas/2026-08-04_2026-08-05/')]
    const h = await openIndex(at('g4', [], undefined, { rev: 'groups' }), '2026-08-05', 'path')
    const spans = await planRects(h, [{ dLo: 0, dHi: 99, pLo: '', pHi: '\uffff' }]) as (Span & { vfMin: number; vfMax: number; vtMin: number; vtMax: number })[]
    const got = spans.map(g => [g.tier ?? 0, g.rg, [g.dMin, g.dMax, g.pMin, g.pMax, g.bMax, g.vfMin, g.vfMax, g.vtMin, g.vtMax, g.rowStart, g.rowEnd]])
    expect(got).toEqual(spans.map(g => [g.tier ?? 0, g.rg, want[g.tier ?? 0].get(g.rg)]))
    // Every run group (a close record matters at any scan after its version opened), and the base's live at 08-05.
    expect([spans.filter(g => g.tier === 1).length, want[1].size]).toEqual([want[1].size, want[1].size])
    expect(spans.filter(g => !g.tier).map(g => g.rg)).toEqual([...want[0]].filter(([, w]) => (w[5] as number) <= h.asOf! && h.asOf! < (w[8] as number)).map(([rg]) => rg))
  })

  it('point lookups find each scan\'s rows, bounded or not, as g1 does', async () => {
    const dates = [...new Set(cases.map(c => c.date))].sort()
    const rows = (rs: Row[]) => rs.map(r => [r.depth, r.path, r.size, r.n_files, r.kind, r.last_read]).sort((x, y) => (`${x[0]}${x[1]}` < `${y[0]}${y[1]}` ? -1 : 1))
    for (const d of dates) {
      const [a, b] = await Promise.all([openIndex(env, d, 'path'), openIndex(at('g3'), d, 'path')])
      const asks: Ask[] = [{ depth: 1, under: '' }, { depth: 2, under: 'b1' }, { depth: 3, under: 'b2/e' }, { depth: 2, path: 'b2/e' }]
      const [x, y] = await Promise.all([readAsks(a, asks, () => true), readAsks(b, asks, () => true)])
      expect({ d, rows: rows(y.rows) }).toEqual({ d, rows: rows(x.rows) })
    }
  })

  it('reads the base and the runs, nothing else: a view of a scan reads the runs begun by then', async () => {
    const got: Record<string, string[]> = {}
    for (const date of [...new Set(cases.map(c => c.date))].sort()) {
      const reads: string[] = []
      // A revision per scan: each scan's reads start from empty isolate caches.
      const e = at('g3', reads, undefined, { rev: `reads-${date}` })
      for (const c of cases.filter(x => x.date === date && x.w === 30 && x.depth == null)) await buildView(e, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2 })
      got[date] = [...new Set(reads)].map(k => k.replace('interval-store/g3/', '')).sort()
    }
    // Every view reads the state and the base's footers. The base's scans read no run; 08-04 reads its own run, 08-05
    // both (where a small subtree's plan picks `path`, a sort's data file is left unread).
    const state = ['manifests/2026-08-05.json', 'scans.json']
    const sort = (d: string, x: string, data = true) => [`${d}served/${x}.groups.parquet`, ...(data ? [`${d}served/${x}.parquet`] : [])]
    const base = (bysize = true) => [...state, ...sort('', 'bysize', bysize), ...sort('', 'path')]
    const run = (d: string, bysize = true) => [...sort(`deltas/${d}/`, 'bysize', bysize), ...sort(`deltas/${d}/`, 'path')]
    expect(got).toEqual({
      '2026-07-30': base().sort(),
      '2026-07-31': base().sort(),
      '2026-08-02': base().sort(),
      '2026-08-03': base(false).sort(),
      '2026-08-04': [...base(), ...run('2026-08-04')].sort(),
      '2026-08-05': [...base(false), ...run('2026-08-04', false), ...run('2026-08-05', false)].sort(),
    })
  })

  it('cuts the stack at a run whose files are missing: its scans leave the store, the ones before it answer as before', async () => {
    // g3's second run listed before its sorts reached R2: its footers (and the data, `head`) are not there.
    const hide = (k: string) => k.startsWith('interval-store/g3/deltas/2026-08-05/')
    const e = at('g3', [], hide, { rev: 'cut1' })
    await expect(openIndex(e, '2026-08-05', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-05")
    const h = await openIndex(e, '2026-08-04', 'path')
    expect((h.runs ?? []).map(r => r.run)).toEqual(['deltas/2026-08-04'])
    const c = cases.find(x => x.date === '2026-08-04' && x.path === '' && x.w === 30 && x.depth == null)!
    expect(flatten((await buildView(e, { date: c.date, path: '', w: 30, h: 30, minArea: 12, atten: 2 })).tree)).toEqual(c.tiles)
  })

  it('cuts a run a read finds broken, and the retry reads around it: never a failed request', async () => {
    // A run's data file gone with its footer still there, on a binding without `head`: only a read finds it. A
    // view or diff reads only the runs begun by its scans (`asOfScans`), so the read here is one of every tier as
    // of a base scan (as `openIndex` alone opens it): the run's close records are read, and its versions tested.
    let gone = true
    const hide = (k: string) => gone && k === 'interval-store/g4/deltas/2026-08-04_2026-08-05/served/path.parquet'
    const at4 = (rev: string) => at('g4', [], hide, { head: false, rev })
    const date = '2026-08-03'
    const rowsAt = async (e: Env) => (await readRows(await openIndex(e, date, 'path'), 1, 99, '', '\uffff')).map(r => [r.depth, r.path, r.size, r.n_files]).sort((x, y) => (`${x[0]}${x[1]}` < `${y[0]}${y[1]}` ? -1 : 1))
    const want = await rowsAt(env)
    expect(want.length).toBeGreaterThan(0)
    // As a request reads it (`ivRetry`): the first try finds the run broken and cuts it, the retry reads the base.
    expect(await ivRetry(() => rowsAt(at4('a')))).toEqual(want)
    // The same read without the retry fails once, then the stack is cut: the run's scans read per-scan (none here:
    // "not synced"), the base's from the base alone.
    await expect(rowsAt(at4('b'))).rejects.toThrow('interval store run deltas/2026-08-04_2026-08-05 is broken')
    await expect(openIndex(at4('b'), '2026-08-05', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-05")
    expect((await openIndex(at4('b'), date, 'path')).runs).toBeUndefined()
    expect(await rowsAt(at4('b'))).toEqual(want)
    // Healed (the file is there again) once the cut expires.
    gone = false
    const t0 = Date.now()
    const now = vi.spyOn(Date, 'now').mockReturnValue(t0 + IV_BROKEN_TTL)
    try {
      expect(((await openIndex(at4('b'), '2026-08-05', 'path')).runs ?? []).map(r => r.run)).toEqual(['deltas/2026-08-04_2026-08-05'])
      expect(await rowsAt(at4('b'))).toEqual(want)
    } finally {
      now.mockRestore()
    }
  })

  it('takes a size key\'s bucket exactly, at every power of two', () => {
    const ks = [0, 1, 2, 3, 49, 52]
    expect(ks.map(k => [sizeBucket(2 ** k - 1), sizeBucket(2 ** k), sizeBucket(2 ** k + 1)])).toEqual([[-1, 0, 1], [0, 1, 1], [1, 2, 2], [2, 3, 3], [48, 49, 49], [51, 52, 52]])
    expect([sizeBucket(0), sizeBucket(-1), sizeBucket(844469911398738)]).toEqual([-1, -1, 49])
  })

  it('bounds a bucket-straddling group by its whole-bucket neighbours', () => {
    // A group of buckets 35 → 34 between a group all of 35 and one all of 34: its 35s follow `b/m`, its 34s precede `a/c`.
    const B = (k: number) => 2 ** k
    const prev = { pMin: 'a/a', pMax: 'b/m', bMin: B(35), bMax: B(35) + 1, seg: 0 }
    const g = { pMin: 'a/a', pMax: 'c/z', bMin: B(34), bMax: B(35) + 9, seg: 0 }
    const next = { pMin: 'a/c', pMax: 'c/c', bMin: B(34), bMax: B(34) + 5, seg: 0 }
    const at = (pLo: string, pHi: string, thr = 0, p: typeof prev | null = prev, n: typeof next | null = next) => sizeNeighbours(g, p ?? undefined, n ?? undefined, [{ pLo, pHi }], thr)
    expect([
      at('a/b/', 'a/b0'), // a 34 (before `a/c`)
      at('a/d/', 'a/d0'), // between: neither bucket's rows
      at('b/x/', 'b/x0'), // a 35 (after `b/m`)
      at('a/b/', 'a/b0', B(35)), // only 35s clear the floor, and they lie after `b/m`
      at('a/d/', 'a/d0', 0, null), // no previous group: 35s from `a/a`
      at('a/d/', 'a/d0', 0, prev, null), // no next group: 34s to `c/z`
      at('a/d/', 'a/d0', 0, { ...prev, seg: 1 }, { ...next, seg: 1 }), // neighbours in another segment
      at('a/d/', 'a/d0', 0, { ...prev, bMin: B(34) }, { ...next, bMax: B(35) }), // neighbours spanning buckets themselves
      sizeNeighbours({ ...g, bMin: undefined }, prev, next, [{ pLo: 'a/d/', pHi: 'a/d0' }], 0), // an older footer: kept
    ]).toEqual([true, false, true, false, true, true, true, true, true])
  })

  it('shapes only the rows a read\'s cheap test passes, as `all` would', () => {
    const raw = [
      { depth: 2, path: 'b1/a', size: 5, vf: 1, vt: 9 },
      { depth: 3, path: 'b1/a/x', size: 50, vf: 1, vt: 9 },
      { depth: 2, path: 'b2/c', size: 70, vf: 2, vt: 9 },
    ]
    const doc = groupDoc(raw, ['depth', 'path', 'size', 'vf', 'vt'])!
    const pre = (d: number, p: string, sz: number) => p.startsWith('b1/') && sz >= 10 * d
    const lazy = new IvGroup({ version: 3 }, doc)
    const picked = lazy.pick(pre)
    expect(picked.map(r => [r.depth, r.path, r.size])).toEqual([[3, 'b1/a/x', 50]])
    expect(picked).toEqual(new IvGroup({ version: 3 }, doc).all().filter(r => pre(r.depth, r.path, r.size)))
    // Shaped once, `pick` filters the shaped rows.
    expect([lazy.all().length, lazy.pick(pre)]).toEqual([3, picked])
  })

  it('reads a size sort without the bucket-straddling groups its neighbours rule out, the same rows', async () => {
    const nodeFs = async () => (await import(/* @vite-ignore */ 'node:fs' as string)) as { readFileSync(p: string): Uint8Array }
    const pq = async (key: string) => {
      const b = (await nodeFs()).readFileSync(fixture(`iv/${key}`))
      const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
      return await parquetReadObjects({ file: ab, metadata: parquetMetadata(ab), compressors }) as Record<string, unknown>[]
    }
    const n = (v: unknown) => Number(v)
    const totals = { cases: 0, candidates: 0, pruned: 0, rows: 0 }
    for (const gen of ['g1', 'g4', 'g5']) {
      const st = await readJson<{ scans: { id: string }[] }>(`iv/interval-store/${gen}/scans.json`)
      const runScans = gen === 'g1' ? [] : gen === 'g4' ? ['2026-08-04', '2026-08-05'] : ['2026-09-30T0009']
      for (const date of [...st.scans.map(x => x.id), ...runScans]) {
        const h = await openIndex(asOfScans(at(gen), [date]), date, 'bysize')
        const tiers = await Promise.all([h, ...(h.runs ?? [])].map(async (t, i) => {
          const dir = `interval-store/${gen}/${i ? `${t.run}/` : ''}served/bysize`
          const groups = (await pq(`${dir}.groups.parquet`)).map(r => ({
            rg: n(r.rg), tier: i || undefined, dMin: n(r.d_min), dMax: n(r.d_max), pMin: String(r.p_min), pMax: String(r.p_max), bMax: n(r.b_max),
            rowStart: n(r.row_start), rowEnd: n(r.row_end), vfMin: n(r.vf_min), vtMax: n(r.vt_max),
          }))
          return { i, groups, rows: await pq(`${dir}.parquet`) }
        }))
        for (const P of ['', 'b1', 'b2', 'b1/d', 'b2/e', 'b2/f']) {
          const dP = P === '' ? 0 : P.split('/').length
          const rects: Rect[] = [P === '' ? { dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' } : { dLo: dP + 1, dHi: 1e9, pLo: `${P}/`, pHi: `${P}0` }]
          for (const T of [0, 5, 10, 20, 40]) {
            const thrAt = (d: number) => T * 2 ** Math.max(0, d - dP - 1)
            const plan = await planSizeRects(h, rects, thrAt)
            // Every group the min/max test selects (liveness as `pqGroups`: a run's group from its first version on).
            const cands = tiers.flatMap(t => t.groups.filter(g => g.vfMin <= h.asOf! && (t.i > 0 || h.asOf! < g.vtMax) && groupMatchesSize(g, rects, thrAt(rects[0].dLo))))
            const key = (s: Span) => `${s.tier ?? 0}:${s.rg}`
            const kept = new Set(plan.map(key))
            const pruned = cands.filter(g => !kept.has(key(g)))
            expect(plan.every(s => cands.some(g => key(g) === key(s)))).toBe(true)
            // A pruned group holds no row the read keeps, of any version.
            const inRead = (r: Record<string, unknown>) => rects.some(q => n(r.depth) >= q.dLo && n(r.depth) <= q.dHi && String(r.path) >= q.pLo && String(r.path) < q.pHi) && n(r.size) >= thrAt(n(r.depth))
            const held = pruned.map(g => tiers[g.tier ?? 0].rows.slice(g.rowStart, g.rowEnd).filter(inRead).map(r => [g.tier ?? 0, g.rg, r.path]))
            expect({ gen, date, P, T, held: held.flat() }).toEqual({ gen, date, P, T, held: [] })
            // The read over the pruned plan is the read over every candidate, row for row.
            const order = (rs: Row[]) => rs.map(r => [r.depth, r.path, r.size, r.vf, r.vt, r.n_files, r.kind]).sort((x, y) => (`${x[0]}\0${x[1]}\0${x[3]}` < `${y[0]}\0${y[1]}\0${y[3]}` ? -1 : 1))
            const [got, want] = await Promise.all([readSizeRects(h, rects, thrAt, undefined, plan), readSizeRects(h, rects, thrAt, undefined, cands)])
            expect({ gen, date, P, T, rows: order(got) }).toEqual({ gen, date, P, T, rows: order(want) })
            totals.cases++
            totals.candidates += cands.length
            totals.pruned += pruned.length
            totals.rows += got.length
          }
        }
      }
    }
    expect(totals).toEqual({ cases: 450, candidates: 1839, pruned: 58, rows: 108595 })
  }, 60_000)  // 450 cases over the fixture's sorts: seconds alone, past the 5 s default under the parallel suite
})
