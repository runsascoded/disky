import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { type OwnerScope, parseQuery } from './scope'
import { type Hit, injectedStores, liveTotal, type HitSource, SuffixHits, staticLiteral, type StaticFilterStore } from './staticFilter'
import { type Blobs, StaticNames } from './staticNames'
import { parseAst } from './querySyntax'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, capTiles, MATCH_LIST_CAP, matchLists, tileBudget, type ViewNode, type View } from './view'
import { searchKey } from './search'
import { resetUpgrades } from './edgeCache'
import { warmSubtree } from '../api/subtree'
import { onRequestGet as diffGet } from '../api/diff'
import type { FilterRejected } from './indexedOnly'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The map's filter from the static name index (`staticFilter.ts`) over `fixtures/static-filter/`
// (`gen.py`): two scans (A, B) of buckets `bk` (3000 filler objects + `tomat` roots at every depth, two
// owners' slices under one dir, objects changing between the scans), `tomato-bk` (a bucket the term
// matches) and `zz`, each a store generation WITH the search sidecars, plus the static index over both.
// The same views are built twice — static (`STATIC_FILTER_STORE`) and from the search sidecars, both
// exact — and checked equal to each other and to brute force from the objects (`expected.json`).

const A = '2026-10-04'
const B = '2026-10-05'
const DATES = [A, B]
const TERMS = ['tomat', 'ttl', 'ckpt', 'safetensors', 'x.bin', 'nomatch']
const ROOTS = ['', 'bk', 'bk/data', 'bk/data/raw', 'bk/data/tomato', 'zz', 'tomato-bk', 'bk/runs/x']
const W = 1280, H = 768, MIN_AREA = 12, ATTEN = 2
const dirOf = (date: string) => `cw-l2/${date}/index/g`
const KEYS = ['shards.json', 'scans.json', 'sx/s0000.parquet', 'sx/s0001.parquet']

type Expected = { roots: Record<string, Record<string, Record<string, [string, number, number][] | null>>>; series: Record<string, Record<string, Record<string, [number, number]>>> }
let expected: Expected
let base: Env
const held = new Map<string, ArrayBuffer>()
function blobs(): Blobs {
  const bytes = (k: string) => { const b = held.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
/** A static store over the fixture: `maxRows` and `heavy` as the dispatch's knobs. */
function staticStore(opts: { maxRows?: number; heavy?: HitSource | null } = {}): StaticFilterStore {
  return { source: new SuffixHits(new StaticNames(blobs()), opts), scans: async () => [...DATES], gen: 'fixture' }
}
const envStatic = (s: StaticFilterStore = staticStore()): Env => { const env = { ...base }; injectedStores.set(env, s); return env }

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of KEYS) { const b = fs.readFileSync(fixture(`static-filter/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  expected = await readJson<Expected>('static-filter/expected.json')
  const { db, raw } = await sqliteD1('cw')
  for (const date of DATES) {
    const v = await readJson<Record<string, D1Variant>>(`static-filter/${date}/d1.json`)
    const files = {
      path: { parquet: `static-filter/${date}/path-index.parquet`, groups: `static-filter/${date}/path-index.groups.json` },
      bysize: { parquet: `static-filter/${date}/path-index-bysize.parquet`, groups: `static-filter/${date}/path-index-bysize.groups.json` },
    }
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
    for (const [role, file] of [['rows', 'rows'], ['trigrams', 'trigrams'], ['rowsSearch', 'rows-search']] as const) {
      FILES.set(searchKey(dirOf(date), role), fixture(`static-filter/${date}/path-index.${file}.parquet`))
    }
  }
  base = { DB: spied(db), ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', SEARCH_SIDECARS: '1' } as Env
})

/** Every D1 span query (`index_row_groups` group select) the views send: counted, and delayed `slowMs` (a
 *  phase 2 whose plans outlast its time budget). The isolate's index handles hold `base`'s DB, so the spy is
 *  installed there, once. */
const spy = { spans: 0, slowMs: 0 }
type Stmt = { bind(...a: unknown[]): Stmt; first(c?: string): Promise<unknown>; all(): Promise<unknown>; run(): Promise<unknown> }
function spied(db: D1Database): D1Database {
  const wrap = (st: Stmt, sql: string): Stmt => ({
    bind: (...a) => wrap(st.bind(...a), sql),
    first: c => st.first(c),
    all: async () => {
      if (sql.startsWith('SELECT rg, d_min')) {
        spy.spans++
        if (spy.slowMs) await new Promise(r => setTimeout(r, spy.slowMs))
      }
      return st.all()
    },
    run: () => st.run(),
  })
  return { prepare: (sql: string) => wrap(db.prepare(sql) as unknown as Stmt, sql), batch: db.batch.bind(db) } as unknown as D1Database
}

const q = (t: string) => parseQuery(t)!
const view = (env: Env, date: string, path: string, t: string, extra: Partial<Parameters<typeof buildView>[1]> = {}) =>
  buildView(env, { date, path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, query: q(t), ...extra })
/** A tree whose `(other)` folds carry no written-time / read-day / class mix: a static view looks up
 *  only the drawn roots' own rows, so a fold of static roots under a synthesized ancestor has bytes and
 *  objects, not those (the search view knows every root's row). */
const sansFoldDetail = (n: ViewNode): ViewNode => {
  const { d: _d, a: _a, cb: _cb, ...rest } = n
  return { ...(n.n === '(other)' ? rest : n), ...(n.c ? { c: n.c.map(sansFoldDetail) } : {}) } as ViewNode
}
/** The view with its tier's phase-1 name dropped (`static+bysize` ~ `search+bysize`). */
const sansP1 = (v: View) => ({ ...v, tier: v.tier.replace(/^(static|search)\+/, '+'), tree: sansFoldDetail(v.tree) })
/** `matched` as brute force's `[path, bytes, objects]`, by path. */
const matchedRows = (v: { matched?: { path: string; b: number; o: number }[] }) =>
  (v.matched ?? []).map(m => [m.path, m.b, m.o]).sort((x, y) => (x[0] < y[0] ? -1 : 1))

/** A tree as `[path, bytes, objects, ('m')?, ('f<n>')?]` rows, depth first (`(other)` under its parent). */
const flatTree = (n: ViewNode, path = '', out: unknown[] = []): unknown[] => {
  out.push([path || '/', n.b, n.o, ...(n.m ? ['m'] : []), ...(n.f != null ? [`f${n.f}`] : [])])
  for (const c of n.c ?? []) flatTree(c, c.n === '(other)' ? `${path}/(other)` : path ? `${path}/${c.n}` : c.n, out)
  return out
}

/** `tomat` at the root on A over a 128×128 canvas (threshold 18,047 · 12 / 128² ≈ 13 bytes at depth 1,
 *  doubling per level): `zz/Checkpoints/TOMAT` (10 bytes) folds with its ancestors `zz/Checkpoints` and
 *  `zz` into the root's residual (10 bytes: under the threshold, so no `(other)` tile); the 77-byte
 *  `tomat-tomat.bin` (depth 4: 106) into `bk/runs/x`'s `(other)` (`f` = 1 fold). Match roots carry
 *  `m`; their insides are phase 2's path-store rows — for the roots with room for them: bytes ≥ T ·
 *  `FILTER_SUBDIV_AREA` / minArea ≈ 4,512 (a 64×64 tile). `tomat-1` (150 bytes, ~136 px²) and
 *  `tomato-bk` (110) are one exact tile each, nothing inside them read. */
const FOLDED: unknown[] = [
  ['/', 18047, 10],
  ['bk', 17927, 7],
  ['bk/runs', 9077, 2],
  ['bk/runs/x', 9077, 2],
  ['bk/runs/x/ckpt-tomat.pt', 9000, 1, 'm'],
  ['bk/runs/x/(other)', 77, 1, 'f1'],
  ['bk/data', 8850, 5],
  ['bk/data/tomato', 8000, 2, 'm'],
  ['bk/data/tomato/a.bin', 5000, 1],
  ['bk/data/tomato/b.bin', 3000, 1],
  ['bk/data/Tomatoes.csv', 700, 1, 'm'],
  ['bk/data/raw', 150, 2],
  ['bk/data/raw/tomat-1', 150, 2, 'm'],
  ['tomato-bk', 110, 2, 'm'],
]

describe('staticLiteral: the queries the static index answers exactly', () => {
  it('one positive substring without `/` (one and two characters too: the drilldown\'s); nothing else', () => {
    const lit = (s: string) => staticLiteral(parseAst(s)!)
    expect(['tomat', 'TOMAT', 'x.bin', 'ab', 'A', '.', 'abc def', 'tom*at', '/tomat', 'tomat -xyz', 'abc|bcd', '"ttl=7d"', '/ttl/'].map(lit))
      .toEqual(['tomat', 'tomat', 'x.bin', 'ab', 'a', '.', null, null, null, null, null, 'ttl=7d', null])
  })
})

describe('static filter: the treemap / table view (`/api/subtree`)', () => {
  for (const date of DATES) for (const root of ROOTS) {
    it(`= the search-sidecar view and brute force, under '${root}' on ${date}`, async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) {
        const [s, r] = await Promise.all([view(envStatic(), date, root, t), view(base, date, root, t)])
        const brute = expected.roots[t][root][date]
        got.push([t, sansP1(s), brute === null ? 'whole' : matchedRows(s)])
        want.push([t, sansP1(r), brute === null ? 'whole' : brute])
        // Served statically (no approximate note) wherever the root doesn't match and anything does.
        if (brute !== null && brute.length) expect([t, s.tier.split('+')[0], s.approximate, s.partial]).toEqual([t, 'static', undefined, undefined])
      }
      expect(got).toEqual(want)
    })
  }

  it('a small canvas folds the roots under its threshold into `(other)` — the same as the search view', async () => {
    const flat = (n: ViewNode, path: string, out: unknown[] = []): unknown[] => {
      out.push([path || '/', n.b, n.o, ...(n.m ? ['m'] : []), ...(n.f != null ? [`f${n.f}`] : [])])
      for (const c of n.c ?? []) flat(c, c.n === '(other)' ? `${path}/(other)` : path ? `${path}/${c.n}` : c.n, out)
      return out
    }
    const small = { w: 128, h: 128 }
    const [s, r] = await Promise.all([view(envStatic(), A, '', 'tomat', small), view(base, A, '', 'tomat', small)])
    expect(sansP1(s)).toEqual(sansP1(r))
    expect(flat(s.tree, '')).toEqual(FOLDED)
    for (const date of DATES) for (const root of ROOTS) for (const t of TERMS) {
      const [s2, r2] = await Promise.all([view(envStatic(), date, root, t, small), view(base, date, root, t, small)])
      expect([date, root, t, sansP1(s2)]).toEqual([date, root, t, sansP1(r2)])
    }
  })

  it('a root holding one object is a leaf (nothing inside it is read)', async () => {
    const v = await view(envStatic(), B, 'bk', 'tomat')
    const node = v.tree.c!.find(c => c.n === 'new-tomat')!
    expect([node.b, node.o, node.k, node.m, node.c]).toEqual([400, 1, 'dir', 1, undefined])
  })

  it('the first paint: the match roots alone, exact, as leaves under their ancestors', async () => {
    const leaves = (n: ViewNode, path: string, out: [string, number, number][] = []): [string, number, number][] => {
      if (n.m) { expect(n.c).toBeUndefined(); out.push([path, n.b, n.o]) }
      for (const c of n.c ?? []) leaves(c, path ? `${path}/${c.n}` : c.n, out)
      return out
    }
    for (const date of DATES) for (const root of ['', 'bk', 'bk/data']) {
      const v = await view(envStatic(), date, root, 'tomat', { firstPaint: true })
      const brute = expected.roots.tomat[root][date]!
      expect([date, root, leaves(v.tree, root).sort((x, y) => (x[0] < y[0] ? -1 : 1)), v.tree.b, v.tier.split('+')[0]]).toEqual([date, root, brute, brute.reduce((n, x) => n + x[1], 0), 'static'])
    }
  })

  it('an owner pool: the same as the search view', async () => {
    for (const owner of ['owned', 'unowned', { not: ['alice'] }] as OwnerScope[]) for (const root of ['', 'bk/data']) {
      const [s, r] = await Promise.all([view(envStatic(), A, root, 'tomat', { owner }), view(base, A, root, 'tomat', { owner })])
      expect([owner, root, sansP1(s)]).toEqual([owner, root, sansP1(r)])
    }
  })
})

describe('static filter: the diff view (`/api/diff`)', () => {
  for (const root of ROOTS) {
    it(`= the search-sidecar diff, under '${root}'`, async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) for (const extra of [{}, { depth: 1 }, { summary: true }]) {
        const o = { from: A, to: B, path: root, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q(t), ...extra }
        const [s, r] = await Promise.all([buildDiff(envStatic(), o), buildDiff(base, o)])
        got.push([t, extra, { ...s, tier: s.tier.replace(/^(static|search)\+/, '+') }])
        want.push([t, extra, { ...r, tier: r.tier.replace(/^(static|search)\+/, '+') }])
      }
      expect(got).toEqual(want)
    })
  }

  it('its matched roots are both scans\' brute-force roots', async () => {
    const d = await buildDiff(envStatic(), { from: A, to: B, path: '', w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q('tomat') })
    const union = new Map<string, [string, number, number]>()
    for (const date of DATES) for (const r of expected.roots.tomat[''][date]!) union.set(r[0], r)
    // Each root once, with the newer scan's numbers where both have it.
    expect(matchedRows(d).map(r => r[0])).toEqual([...union.keys()].sort())
  })
})

describe('static filter: size over time (`/api/series`)', () => {
  it('Σ each scan\'s own match roots under the path = brute force', async () => {
    const src = staticStore().source
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) for (const root of ROOTS) {
      if (expected.roots[t][root][A] === null) continue
      const hits = (await src.hits(t, root))!.hits!
      got.push([t, root, Object.fromEntries(DATES.map(d => { const x = liveTotal(hits, d); return [d, [x.b, x.o]] }))])
      want.push([t, root, expected.series[t][root]])
    }
    expect(got).toEqual(want)
  })
})

describe('matchLists: a response lists the tree\'s roots, then the heaviest, up to the cap', () => {
  it('under the cap: all, with the count; over it: kept + heaviest, capped', () => {
    const m = (i: number) => ({ path: `p${String(i).padStart(5, '0')}`, b: 10_000 - i, o: 1 })
    const few = [m(0), m(1)]
    expect(matchLists(few, new Map())).toEqual({ matches: ['p00000', 'p00001'], matched: few, matchCount: { n: 2, b: 19_999, o: 2 } })
    const many = Array.from({ length: MATCH_LIST_CAP + 3 }, (_, i) => m(i))
    const got = matchLists(many, new Map([[m(MATCH_LIST_CAP + 2).path, 1]]))
    expect([got.matched!.length, got.matched!.at(-1), got.matched!.at(-2), got.matchCount, got.matchesCapped]).toEqual([
      MATCH_LIST_CAP + 1, m(MATCH_LIST_CAP + 2), m(MATCH_LIST_CAP - 1),
      { n: MATCH_LIST_CAP + 3, b: many.reduce((n, x) => n + x.b, 0), o: MATCH_LIST_CAP + 3 }, true,
    ])
  })
})

describe('static filter: dispatch', () => {
  it('a range over `maxRows` asks the heavy source; its decline is `scan-not-indexed` (a member its drill\'s live tiers don\'t know yet), never the walk', async () => {
    const asked: [string, string][] = []
    const none: HitSource = { async hits(key, under) { asked.push([key, under]); return null } }
    const got = await view(envStatic(staticStore({ maxRows: 3, heavy: none })), A, 'bk', 'tomat').catch((e: FilterRejected) => e.reject.code)
    expect([asked, got]).toEqual([[['tomat', 'bk']], 'scan-not-indexed'])
  })

  it('the heavy source\'s hits serve the view statically', async () => {
    const light = staticStore().source
    const heavy: HitSource = { hits: (key, under) => light.hits(key, under) }
    const v = await view(envStatic(staticStore({ maxRows: 3, heavy })), B, '', 'tomat')
    expect([v.tier.split('+')[0], matchedRows(v)]).toEqual(['static', expected.roots.tomat[''][B]])
  })

  it('a literal on a scan outside the generation is `scan-not-indexed`, never the walk; a class scope keeps today\'s read', async () => {
    const outside: StaticFilterStore = { ...staticStore(), scans: async () => [B] }
    const got = await view(envStatic(outside), A, '', 'tomat').catch((e: FilterRejected) => e.reject.code)
    const c = await view(envStatic(), A, '', 'tomat', { classes: new Set(['1']) as never })
    expect([got, c.tier.split('+')[0]]).toEqual(['scan-not-indexed', 'search'])
  })

  it('a first hit dedups a name holding the term twice (one row per occurrence in the shards)', async () => {
    const hits = (await staticStore().source.hits('tomat', 'bk/runs/x'))!.hits!
    const key = (h: Hit) => `${h.path} ${h.usr} ${h.vf}`
    expect(hits.map(key).sort()).toEqual([
      `bk/runs/x/ckpt-tomat.pt alice ${Date.UTC(2026, 9, 4)}`, `bk/runs/x/ckpt-tomat.pt alice ${Date.UTC(2026, 9, 5)}`, `bk/runs/x/tomat-tomat.bin  ${Date.UTC(2026, 9, 4)}`,
    ])
  })
})


describe('static filter: a bounded view — the canvas\'s tiles, exact totals', () => {
  /** `tomat` at the root on A, every node drawn (1280×768: threshold ≈ 0.22 bytes, so only the budgets bound it). */
  const FULL: unknown[] = [
    ['/', 18047, 10],
    ['bk', 17927, 7],
    ['bk/runs', 9077, 2],
    ['bk/runs/x', 9077, 2],
    ['bk/runs/x/ckpt-tomat.pt', 9000, 1, 'm'],
    ['bk/runs/x/tomat-tomat.bin', 77, 1, 'm'],
    ['bk/data', 8850, 5],
    ['bk/data/tomato', 8000, 2, 'm'],
    ['bk/data/tomato/a.bin', 5000, 1],
    ['bk/data/tomato/b.bin', 3000, 1],
    ['bk/data/Tomatoes.csv', 700, 1, 'm'],
    ['bk/data/raw', 150, 2],
    ['bk/data/raw/tomat-1', 150, 2, 'm'],
    ['bk/data/raw/tomat-1/x.bin', 100, 1],
    ['bk/data/raw/tomat-1/TOMAT-inner', 50, 1],
    ['bk/data/raw/tomat-1/TOMAT-inner/y.bin', 50, 1],
    ['tomato-bk', 110, 2, 'm'],
    ['tomato-bk/a', 100, 1],
    ['tomato-bk/a/b.bin', 100, 1],
    ['tomato-bk/c', 10, 1],
    ['tomato-bk/c/tomat.txt', 10, 1],
    ['zz', 10, 1],
    ['zz/Checkpoints', 10, 1],
    ['zz/Checkpoints/TOMAT', 10, 1, 'm'],
  ]
  /** The drawn nodes, heaviest first (ties shallowest first, then by path): what a budget of k keeps. */
  const byWeight = (rows: unknown[]) => (rows as [string, number][])
    .filter(([p]) => p !== '/' && !p.endsWith('(other)'))
    .sort((x, y) => y[1] - x[1] || x[0].split('/').length - y[0].split('/').length || (x[0] < y[0] ? -1 : 1))
    .map(([p]) => p)

  it('unbounded: every node', async () => {
    const v = await view(envStatic(), A, '', 'tomat', { maxTiles: Infinity })
    expect([v.nodes, v.interiors, flatTree(v.tree)]).toEqual([23, undefined, FULL])
  })

  it('a budget of k draws the k heaviest nodes; the rest fold into `(other)`, totals unchanged', async () => {
    const full = await view(envStatic(), A, '', 'tomat', { maxTiles: Infinity })
    const order = byWeight(FULL)
    for (const k of [1, 2, 4, 6, 10, 15, 22]) {
      const v = await view(envStatic(), A, '', 'tomat', { maxTiles: k })
      const drawn = byWeight(flatTree(v.tree))
      expect([k, v.nodes, [...drawn].sort(), v.tree.b, v.tree.o, v.matchCount, v.matched])
        .toEqual([k, k, order.slice(0, k).sort(), full.tree.b, full.tree.o, full.matchCount, full.matched])
    }
    expect(flatTree((await view(envStatic(), A, '', 'tomat', { maxTiles: 4 })).tree)).toEqual([
      ['/', 18047, 10],
      ['bk', 17927, 7],
      ['bk/runs', 9077, 2],
      ['bk/runs/x', 9077, 2],
      ['bk/runs/x/ckpt-tomat.pt', 9000, 1, 'm'],
      ['bk/runs/x/(other)', 77, 1, 'f1'],
      ['bk/(other)', 8850, 5, 'f1'],
      ['/(other)', 120, 3, 'f2'],
    ])
  })

  it('the default budget is w·h / 96 px² per tile', () => {
    expect([tileBudget(1280, 768), tileBudget(512, 384), tileBudget(1, 1)]).toEqual([10_240, 2_048, 1])
  })

  it('capTiles: the heaviest, ancestor-closed (a node whose parent fell out goes too), ties shallowest first', () => {
    const kept = new Map(Object.entries({ a: { b: 10 }, 'a/x': { b: 10 }, 'a/x/1': { b: 6 }, 'a/y': { b: 4 }, b: { b: 7 }, 'b/z': { b: 7 } }))
    const depth = new Map([...kept.keys()].map(p => [p, p.split('/').length]))
    const k3 = new Map(kept)
    expect([capTiles(k3, depth, '', 3), [...k3.keys()]]).toEqual([['b/z', 'a/x/1', 'a/y'], ['a', 'a/x', 'b']])
    const k5 = new Map(kept)
    expect([capTiles(k5, depth, '', 5), [...k5.keys()]]).toEqual([['a/y'], ['a', 'a/x', 'a/x/1', 'b', 'b/z']])
    // A child heavier than its parent (data that disagrees with itself) never leaves an orphan.
    const odd = new Map(Object.entries({ a: { b: 1 }, 'a/x': { b: 9 }, b: { b: 5 } }))
    expect([capTiles(odd, new Map([['a', 1], ['a/x', 2], ['b', 1]]), '', 2), [...odd.keys()]]).toEqual([['a/x', 'a'], ['b']])
    const none = new Map(kept)
    expect([capTiles(none, depth, '', 6), none.size]).toEqual([[], 6])
  })

  it('`depth=N` (a diff\'s first level): nothing inside the roots below dP + N is read', async () => {
    const v = await view(envStatic(), A, '', 'tomat', { maxDepth: 2 })
    const inside = /^(bk\/data\/tomato|bk\/data\/raw\/tomat-1|tomato-bk\/a|tomato-bk\/c)\//
    expect([v.interiors, flatTree(v.tree)]).toEqual([undefined, FULL.filter(r => !inside.test((r as [string])[0]))])
  })

  it('phase 2 over its read budget: the subdividable roots are drawn whole and the response says so', async () => {
    const byRows = await view(envStatic(), A, '', 'tomat', { phase2Rows: 0 })
    const v = await view(envStatic(), A, '', 'tomat', { phase2Groups: 0 })
    expect(byRows).toEqual(v)
    const full = await view(envStatic(), A, '', 'tomat', { maxTiles: Infinity })
    expect([v.interiors, v.tree.b, v.matchCount, v.matched]).toEqual([{ read: 0, skipped: 3, reason: '3 over the read budget' }, full.tree.b, full.matchCount, full.matched])
    expect(flatTree(v.tree)).toEqual(FULL.filter(r => !/^(bk\/data\/tomato|bk\/data\/raw\/tomat-1|tomato-bk)\//.test((r as [string])[0])))
  })
})

describe('a view whose root the query matches (NOT): its own forest', () => {
  // Phase 2 kept a row only under a drawn match root other than the view root, so these drew empty.
  it('`tomat -nomatch` at `bk/data/tomato`: the root and what is inside it', async () => {
    const v = await view(base, A, 'bk/data/tomato', 'tomat -nomatch')
    expect([v.matches, v.excluded, flatTree(v.tree)]).toEqual([
      ['bk/data/tomato'], undefined,
      [['/', 8000, 2, 'm'], ['a.bin', 5000, 1], ['b.bin', 3000, 1]],
    ])
  })
  it('`tomat -a.bin` at `bk/data/tomato`: the root less its excluded file (one object left: a leaf)', async () => {
    const v = await view(base, A, 'bk/data/tomato', 'tomat -a.bin')
    expect([v.matches, v.excluded, flatTree(v.tree)]).toEqual([['bk/data/tomato'], ['bk/data/tomato/a.bin'], [['/', 3000, 1, 'm']]])
  })
})

describe('phase 2: a depth\'s roots over the budget together', () => {
  // Two match roots at one depth (`bk/data/tomato`, `bk/data/raw`; served as static hits), one read.
  const two: HitSource = {
    async hits() {
      const h = (path: string, size: number, n: number): Hit => ({ path, depth: 3, usr: '', vf: 0, vt: 9e15, size: BigInt(size), n: BigInt(n) })
      return { hits: [h('bk/data/tomato', 8000, 2), h('bk/data/raw', 183, 3)], io: { from: 'test' } }
    },
  }
  const env = () => envStatic({ source: two, scans: async () => [...DATES], gen: 'two' })
  it('in budget: both subdivided; over it: each planned alone, and each counted once', async () => {
    const all = await view(env(), A, '', 'tomat')
    const none = await view(env(), A, '', 'tomat', { phase2Groups: 0 })
    expect([all.interiors, flatTree(all.tree), none.interiors, flatTree(none.tree)]).toEqual([
      undefined,
      [['/', 8183, 5], ['bk', 8183, 5], ['bk/data', 8183, 5], ['bk/data/tomato', 8000, 2, 'm'], ['bk/data/tomato/a.bin', 5000, 1], ['bk/data/tomato/b.bin', 3000, 1],
        ['bk/data/raw', 183, 3, 'm'], ['bk/data/raw/tomat-1', 150, 2], ['bk/data/raw/tomat-1/x.bin', 100, 1], ['bk/data/raw/tomat-1/TOMAT-inner', 50, 1],
        ['bk/data/raw/tomat-1/TOMAT-inner/y.bin', 50, 1], ['bk/data/raw/plain.bin', 33, 1]],
      { read: 0, skipped: 2, reason: '2 over the read budget' },
      [['/', 8183, 5], ['bk', 8183, 5], ['bk/data', 8183, 5], ['bk/data/tomato', 8000, 2, 'm'], ['bk/data/raw', 183, 3, 'm']],
    ])
  })
})

describe('phase 2: the request\'s gate (`Phase2Gate`)', () => {
  // `tomat` at the root on A: phase 1 from the static hits, then phase 2 over its 3 subdividable roots.
  const run = async (extra: Partial<Parameters<typeof buildView>[1]>, slowMs = 0) => {
    spy.spans = 0
    spy.slowMs = slowMs
    try {
      const v = await view(envStatic(), A, '', 'tomat', extra)
      return { v, spans: spy.spans }
    } finally { spy.slowMs = 0 }
  }
  const whole = /^(bk\/data\/tomato|bk\/data\/raw\/tomat-1|tomato-bk)\//

  it('a dead gate: phase 2 skipped outright — no span query of its own, the roots drawn whole, totals exact', async () => {
    const open = await run({ phase2Gate: { dead: false } })
    const shut = await run({ phase2Gate: { dead: true } })
    const full = await run({ maxTiles: Infinity })
    expect([shut.v.interiors, shut.v.tree.b, shut.v.tree.o, shut.v.matchCount, shut.v.matched]).toEqual([
      { read: 0, skipped: 3, reason: '3 past the time budget', late: 3 },
      full.v.tree.b, full.v.tree.o, full.v.matchCount, full.v.matched,
    ])
    expect(flatTree(shut.v.tree)).toEqual(flatTree(full.v.tree).filter(r => !whole.test((r as [string])[0])))
    // Only the root details' lookups query spans under a dead gate; an open one plans phase 2 as well.
    expect([open.v.interiors, open.spans, shut.spans]).toEqual([undefined, 7, 4])
  })

  it('a round that reads nothing in its time shuts the gate; one that reads, or ends on the read budget, leaves it open', async () => {
    const late = { dead: false }
    const cut = await run({ phase2Gate: late, phase2Ms: 5 }, 40)
    const read = { dead: false }
    await run({ phase2Gate: read })
    const budget = { dead: false }
    await run({ phase2Gate: budget, phase2Groups: 0 })
    expect([cut.v.interiors, late.dead, read.dead, budget.dead]).toEqual([
      { read: 0, skipped: 3, reason: '3 past the time budget', late: 3 }, true, false, false,
    ])
  })
})

describe('the diff shares one gate across its reads', () => {
  it('both sides\' phase 2 late: the sides drawn whole, budget-cut (`interiors.late`), totals as the full diff', async () => {
    spy.slowMs = 40
    const gate = { dead: false }
    let cut
    try {
      // A diff's phase 2 budget is the deployment's (`FILTER_PHASE2_MS`), capped at `FILTER_DIFF_PHASE2_MS`.
      const e = envStatic()
      e.FILTER_PHASE2_MS = '5'
      cut = await buildDiff(e, { from: A, to: B, path: '', w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 500, query: q('tomat'), phase2Gate: gate })
    } finally { spy.slowMs = 0 }
    const full = await buildDiff(envStatic(), { from: A, to: B, path: '', w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 500, query: q('tomat') })
    expect([gate.dead, cut.interiors, full.interiors, cut.total_a, cut.total_b, cut.matchCount]).toEqual([true, { read: 0, skipped: 6, reason: '3 past the time budget', late: 6 }, undefined, full.total_a, full.total_b, full.matchCount])
  })
})

describe('a budget-cut answer upgrades to the full one in the background (`upgradePartial`)', () => {
  // The viewer-facing phase 2 gets 5 ms against 40 ms span queries (cut); the background run's
  // `UPGRADE_PHASE2_MS` doesn't. A colo cache that keeps what it's given; `waitUntil` collects the runs.
  const harness = () => {
    const held = new Map<string, Response>()
    const prev = (globalThis as unknown as { caches: unknown }).caches
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { held.set(k.url, r) },
    } }
    const bg: Promise<unknown>[] = []
    const env = envStatic()
    ;(env as unknown as Record<string, string>).FILTER_PHASE2_MS = '5'
    ;(env as unknown as Record<string, string>).DEV_EMAIL = 'dev@example.test'
    ;(env as unknown as Record<string, string>).STORE_BUCKET = 'my-data'
    resetUpgrades()
    spy.slowMs = 40
    const restore = () => { spy.slowMs = 0; (globalThis as unknown as { caches: unknown }).caches = prev }
    return { env, bg, waitUntil: (p: Promise<unknown>) => { bg.push(p) }, restore }
  }
  /** What a response says about its answer: cache tier, partial flag, upgrade, and the body's cut / interiors. */
  const said = async (r: Response) => {
    const b = await r.json() as { budgetCut?: true; interiors?: unknown }
    return [r.headers.get('x-cache'), r.headers.get('x-cache-partial'), r.headers.get('x-cache-upgrade'), b.budgetCut ?? null, b.interiors ?? null]
  }

  it('subtree: the miss serves the partial and schedules the run; a partial hit meanwhile is deduped; then the hit is whole', async () => {
    const { env, bg, waitUntil, restore } = harness()
    const url = `http://localhost/api/subtree?date=${A}&path=&q=tomat&full=1&w=${W}&h=${H}&minArea=${MIN_AREA}&atten=${ATTEN}`
    try {
      const miss = await said(await warmSubtree(env, url, waitUntil))
      const during = await said(await warmSubtree(env, url, waitUntil))
      const runs = bg.length
      await Promise.all(bg)
      const after = await warmSubtree(env, url, waitUntil)
      const body = await after.clone().json() as { tree: ViewNode }
      const full = await view(envStatic(), A, '', 'tomat')
      expect([miss, during, runs, await said(after), body.tree]).toEqual([
        ['miss', 'ttl=120', 'scheduled', true, { read: 0, skipped: 3, reason: '3 past the time budget', late: 3 }],
        ['hit', 'ttl=120', 'in-flight', true, { read: 0, skipped: 3, reason: '3 past the time budget', late: 3 }],
        2, // the miss's cache put, and the run
        ['hit', null, null, null, null],
        JSON.parse(JSON.stringify(full.tree)),
      ])
    } finally { restore() }
  })

  it('diff: the same — the background run\'s `phase2Ms` reaches both sides', async () => {
    const { env, bg, waitUntil, restore } = harness()
    const url = `http://localhost/api/diff?from=${A}&to=${B}&path=&q=tomat&w=${W}&h=${H}&minArea=${MIN_AREA}&atten=${ATTEN}`
    const get = () => diffGet({ request: new Request(url), env, waitUntil })
    try {
      const miss = await get()
      const missSaid = await said(miss)
      await Promise.all(bg)
      const after = await get()
      expect([missSaid.slice(0, 4), await said(after)]).toEqual([
        ['miss', 'ttl=120', 'scheduled', true],
        ['hit', null, null, null, null],
      ])
    } finally { restore() }
  })
})
