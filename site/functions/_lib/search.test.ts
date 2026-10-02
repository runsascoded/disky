import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { matchRoots } from './filter'
import { type IndexHandle, openIndex, readRects, type Row } from './index'
import { type NamePred, parseQuery as parseWith } from './scope'
import { SEARCH_FILES, type SearchLimits, SEARCH_LIMITS, searchKey, searchRoots } from './search'
import { makeSimple, regex } from './querySyntax'
import { planPositive } from './searchQuery'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, GETS, readJson, seedGeneration } from './testStore'
import { APPROX_EXCL_NO_INDEX, APPROX_NO_INDEX, APPROX_UNINDEXED, buildDiff, buildView, type View } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The search index reader (specs/path-store-search.md §4) over
// `fixtures/v2-search/` (`gen.py` `write_v2_search`): buckets `bk` (6000
// filler objects `fill/f*` + `ttl` dirs nested in `ttl` dirs, case variants,
// `.safetensors` objects, `ckpt…final` paths, a Kelvin-sign `Key`) and `zz`,
// `path` + `bysize` sorts in 2048-row groups (3 each), the search sidecars in
// both layouts (v2: 512-row rows groups; v1: 2048-row names groups; the same
// 2048-row postings) at 2 rows per directory group. Served with the v2
// sidecars (SEARCH, and SEARCH_PQ with its groups retired to the blob), the
// v1 ones (SEARCH_V1) and none (PLAIN: the same generation, the pre-index
// read).

const SEARCH = '2026-10-01T0001'
const PLAIN = '2026-10-01T0002'
const SEARCH_PQ = '2026-10-01T0003'
const SEARCH_V1 = '2026-10-01T0004'
const V2_FILES = ['rows', 'trigrams', 'rowsSearch'] as const
const V1_FILES = ['names', 'trigrams', 'search'] as const
const dirOf = (date: string) => `cw-l2/${date}/index/g`
const files = { path: { parquet: 'v2-search/path-index.parquet', groups: 'v2-search/path-index.groups.json' }, bysize: { parquet: 'v2-search/path-index-bysize.parquet', groups: 'v2-search/path-index-bysize.groups.json' } }
const MiB = 1 << 20
// The engine's own cases include short needles (`gr`, `zz`): `simple`
// without its 3-character minimum (the predicate is the same).
const LAX = makeSimple({ minTerm: 1 })
const parseQuery = (q: string, syntax = LAX) => parseWith(q, syntax)
let env: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const v = await readJson<Record<string, D1Variant>>('v2-search/d1.json')
  for (const date of [SEARCH, PLAIN, SEARCH_V1]) seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
  // Retired from D1: the `path` groups' metadata comes from the blob.
  seedGeneration(raw, { date: SEARCH_PQ, gen: 'g', dir: dirOf(SEARCH_PQ), variants: v, files, retired: ['path', 'bysize'] })
  const sidecar = (date: string, roles: readonly (keyof typeof SEARCH_FILES)[]) => {
    for (const role of roles) FILES.set(searchKey(dirOf(date), role), fixture(`v2-search/${SEARCH_FILES[role]}`))
  }
  for (const date of [SEARCH, SEARCH_PQ]) sidecar(date, V2_FILES)
  sidecar(SEARCH_V1, V1_FILES)
  env = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

const allRows = async (date: string): Promise<Row[]> => readRects(await openIndex(env, date), [{ dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' }])
const under = (root: string) => (p: string) => root === '' || p.startsWith(root + '/')
/** `matchRoots` over every row of the store under `root` — what the index must reproduce. */
async function brute(q: string, root: string): Promise<string[]> {
  const rows = await allRows(PLAIN)
  return matchRoots(rows.map(r => r.path).filter(under(root)), parseQuery(q)!, root)
}
/** The positive search for `pred` (null: the index declines or has no sidecar). */
async function find(h: IndexHandle, pred: NamePred, root: string, limits?: SearchLimits) {
  const plan = planPositive(pred.ast!)
  return plan ? searchRoots(env, h, pred, plan, root, limits) : null
}
/** A view without its coverage flags, and the flags' reasons. */
const bare = ({ partial: _p, partialReason: _pr, approximate: _a, approximateReason: _ar, ...v }: View) => v
const coverage = (v: View) => ({ partial: v.partialReason, approximate: v.approximateReason })
const byPath = (rows: Row[]) => [...rows].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : a.usr! < b.usr! ? -1 : 1))

describe('searchRoots: exactly the outermost matches', () => {
  it('a substring at the root: every `ttl` dir and object, nested ones collapsed, any case', async () => {
    const got = (await find(await openIndex(env, SEARCH), parseQuery('ttl')!, ''))!
    expect(got.roots).toEqual(['bk/fill/zz-TTL-b', 'bk/fill/zz-ttl-a', 'bk/iris/TTL-misc', 'bk/tmp/ttl=14d', 'bk/tmp/ttl=7d', 'zz/Checkpoints/ttl'])
    expect(got.truncated).toBe(false)
    // The roots' own rows, every one: phase 1's aggregates come from them.
    const all = await allRows(PLAIN)
    expect(byPath(got.rows)).toEqual(byPath(all.filter(r => got.roots.includes(r.path))))
    // v2: 1 trigram in 1 postings group → the same 7 candidates, whose rows
    // live in 3 rows groups (ids 7–24 in group 0, `TTL-misc` and `inner-ttl`
    // in 2, `zz/Checkpoints/ttl` in 7: 3 directory groups, 1 more for the
    // trigram) holding matching rows of 14 names (the roots' descendants
    // too); no names file, no `path` group.
    expect(got.stats).toEqual({ layout: 2, mode: 'trigrams', trigrams: 1, postingsRgs: 1, candidates: 7, dirGroups: 4, namesRgs: 0, names: 14, pathRgs: 0, rowsRgs: 3, lifted: 0 })
    const v1 = (await find(await openIndex(env, SEARCH_V1), parseQuery('ttl')!, ''))!
    expect([v1.roots, byPath(v1.rows)]).toEqual([got.roots, byPath(got.rows)])
    // v1: the same 7 candidates (`ttl`, `TTL-misc`, `inner-ttl`, `ttl=14d`,
    // `ttl=7d`, `zz-ttl-a`, `zz-TTL-b`) in 2 names groups, all verified,
    // living in 1 `path` group; 2 directory groups read.
    expect(v1.stats).toEqual({ layout: 1, mode: 'trigrams', trigrams: 1, postingsRgs: 1, candidates: 7, dirGroups: 2, namesRgs: 2, names: 7, pathRgs: 1, rowsRgs: 0, lifted: 0 })
  })

  const QUERIES = [
    'ttl', 'TTL', 'ttl=14d', 'ttl|safetensors', 'run-a/ckpt', 'tmp/ttl', 'swarm', '.safetensors', 'key', 'checkpoints', 'ckpt', 'gr', 'f0042', 'zz-',
    'ckpt final', 'grug swarm', 'ckpt*final', 'model-*-of-*.safetensors', 'tmp/*/ckpt', 'f00*1', '"ttl=7d"', 'TTL -14d', 'ttl -fill|safetensors', 'models -llama',
  ]
  for (const [layout, date] of [['v2', SEARCH], ['v1', SEARCH_V1]]) for (const root of ['', 'bk', 'bk/tmp', 'bk/iris', 'zz']) {
    it(`= matchRoots over every row, under '${root}' (${layout})`, async () => {
      const h = await openIndex(env, date)
      const got: unknown[] = []
      const want: unknown[] = []
      for (const q of QUERIES) {
        const pred = parseQuery(q)!
        if (pred(root)) continue // the whole view matches: no search runs
        const f = await find(h, pred, root)
        got.push([q, f?.roots, f?.truncated])
        want.push([q, await brute(q, root), false])
      }
      expect(got).toEqual(want)
    })
  }

  it('the cold-footer copy (row-group metadata from the blob) answers the same', async () => {
    const [d1, pq] = await Promise.all([openIndex(env, SEARCH), openIndex(env, SEARCH_PQ)])
    expect(pq.mode).toBe('blob')
    for (const q of ['ttl', 'safetensors', 'gr']) {
      expect((await find(pq, parseQuery(q)!, ''))!.roots).toEqual((await find(d1, parseQuery(q)!, ''))!.roots)
    }
  })

  it('serves what it can, declines the rest (null = the view reads as before)', async () => {
    const h = await openIndex(env, SEARCH)
    // A term ending in `/`, the regex fallback, a query of only negatives.
    for (const q of ['ckpt/', '/ckpt.*final/', '-fill']) expect([q, await find(h, parseQuery(q)!, '')]).toEqual([q, null])
    // No sidecar for the generation.
    expect(await find(await openIndex(env, PLAIN), parseQuery('ttl')!, '')).toBeNull()
  })

  it('a short needle has no trigram: the names scan, heaviest names first', async () => {
    const got = (await find(await openIndex(env, SEARCH), parseQuery('gr')!, ''))!
    expect([got.roots, got.truncated, got.stats.mode]).toEqual([['bk/runs/grug'], false, 'scan'])
  })

  it('no candidate: no `path` group is read', async () => {
    const h = await openIndex(env, SEARCH)
    const before = GETS.length
    const got = (await find(h, parseQuery('qqqzzz')!, ''))!
    expect([got.roots, got.truncated, got.stats.candidates, got.stats.pathRgs]).toEqual([[], false, 0, 0])
    expect(GETS.slice(before).filter(g => g.key.endsWith('/path-index.parquet'))).toEqual([])
  })
})

describe('budgets: a cut search is flagged, heaviest names kept, roots still outermost', () => {
  const f0598 = (...ids: string[]) => ids.map(i => `bk/fill/f0${i}`)
  it('v2 rows groups: the heaviest names’ groups first, a name whole or not at all', async () => {
    const h = await openIndex(env, SEARCH)
    // `0598`'s 11 names sit in 9 rows groups, heaviest first: `f05987` (2 KiB,
    // id 523) and `f00598` (1 KiB, 574) in group 1, `f05986` (1023) in 2, ….
    const got = (await find(h, parseQuery('0598')!, '', { ...SEARCH_LIMITS, rowsRgs: 1 }))!
    expect([got.roots, got.truncated, got.reason, got.stats.rowsRgs, got.stats.lifted]).toEqual([f0598('0598', '5987'), true, 'the row read hit its budget (1 row group)', 1, 0])
    const two = (await find(h, parseQuery('0598')!, '', { ...SEARCH_LIMITS, rowsRgs: 2 }))!
    expect([two.roots, two.stats.rowsRgs]).toEqual([f0598('0598', '5985', '5986', '5987'), 2])
    expect((await find(h, parseQuery('0598')!, ''))!.roots).toEqual(await brute('0598', ''))
  })
  it('v2: a cut ancestor is lifted from the `path` sort', async () => {
    const h = await openIndex(env, SEARCH)
    // `zz` (2 characters: the names scan) in rows group 0 only: `zz-TTL-b`,
    // `zz-ttl-a` and rows under the bucket `zz`, whose own row (id 3538,
    // group 6) was not read — one point lookup fetches it.
    const got = (await find(h, parseQuery('zz')!, '', { ...SEARCH_LIMITS, rowsRgs: 1 }))!
    expect([got.roots, got.truncated, got.reason, got.stats.mode, got.stats.rowsRgs, got.stats.lifted]).toEqual([await brute('zz', ''), true, 'the row read hit its budget (1 row group)', 'scan', 1, 1])
    expect(byPath(got.rows)).toEqual(byPath((await allRows(PLAIN)).filter(r => got.roots.includes(r.path))))
    const dropped = (await find(h, parseQuery('zz')!, '', { ...SEARCH_LIMITS, rowsRgs: 1, liftGroups: 0 }))!
    expect([dropped.roots, dropped.reason]).toEqual([['bk/fill/zz-TTL-b', 'bk/fill/zz-ttl-a'], 'the row read hit its budget (1 row group); 1 match root too wide to look up'])
  })
  it('the rows kept: past `keptRows` no further group is decoded', async () => {
    // v2, in id order: group 1 keeps 2 rows (`f05987`, `f00598`), group 2
    // two more (`f05986`, `f05985`); then the search stops.
    const v2 = (await find(await openIndex(env, SEARCH), parseQuery('0598')!, '', { ...SEARCH_LIMITS, keptRows: 2 }))!
    expect([v2.roots, v2.truncated, v2.reason, v2.stats.rowsRgs]).toEqual([f0598('0598', '5985', '5986', '5987'), true, 'more than 2 matching rows', 2])
    // v1, in `path` order: group 0 keeps `f00598`; group 2 is not decoded.
    const v1 = (await find(await openIndex(env, SEARCH_V1), parseQuery('0598')!, '', { ...SEARCH_LIMITS, keptRows: 0 }))!
    expect([v1.roots, v1.truncated, v1.reason, v1.stats.pathRgs]).toEqual([f0598('0598'), true, 'more than 0 matching rows', 1])
  })
  it('the wall clock: past `wallMs` the search stops where it is', async () => {
    for (const date of [SEARCH, SEARCH_V1]) {
      const got = (await find(await openIndex(env, date), parseQuery('ttl')!, '', { ...SEARCH_LIMITS, wallMs: 0 }))!
      expect([date, got.roots, got.truncated, got.reason]).toEqual([date, [], true, 'the search hit its time budget (0.0 s)'])
    }
  })
  it('v1 `path` groups: the heaviest names’ groups first', async () => {
    const h = await openIndex(env, SEARCH_V1)
    // `0598`: `f05980`..`f05989` (path group 2) and `f00598` (group 0). By
    // bytes: `f05987` (2 KiB), then `f00598` and `f05986` (1 KiB, by name) —
    // `f00598` needs a second group, so the search stops there. Every match
    // in the group it read comes back, lighter names' included.
    const got = (await find(h, parseQuery('0598')!, '', { ...SEARCH_LIMITS, pathRgs: 1 }))!
    expect([got.roots, got.truncated, got.stats.pathRgs, got.stats.lifted]).toEqual([Array.from({ length: 10 }, (_, i) => `bk/fill/f0598${i}`), true, 1, 0])
    expect(got.reason).toBe('the row read hit its budget (1 path group)')
    expect((await find(h, parseQuery('0598')!, ''))!.roots).toEqual(await brute('0598', ''))
  })
  it('v1: a cut ancestor is lifted: its rows come from one point lookup', async () => {
    const h = await openIndex(env, SEARCH_V1)
    // `zz-TTL-b`, `zz-ttl-a` (path group 2) outweigh the bucket `zz` (group
    // 0): only group 2 is read, where `zz/…` rows match too — their outermost
    // match, the bucket, is fetched by itself.
    const got = (await find(h, parseQuery('zz')!, '', { ...SEARCH_LIMITS, pathRgs: 1 }))!
    expect([got.roots, got.truncated, got.stats.pathRgs, got.stats.lifted]).toEqual([await brute('zz', ''), true, 1, 1])
    expect(got.roots).toEqual(['bk/fill/zz-TTL-b', 'bk/fill/zz-ttl-a', 'zz'])
    expect(got.reason).toBe('the row read hit its budget (1 path group)')
    // No lookup budget: the ancestor is dropped, and the reason says so.
    const dropped = (await find(h, parseQuery('zz')!, '', { ...SEARCH_LIMITS, pathRgs: 1, liftGroups: 0 }))!
    expect([dropped.roots, dropped.reason]).toEqual([['bk/fill/zz-TTL-b', 'bk/fill/zz-ttl-a'], 'the row read hit its budget (1 path group); 1 match root too wide to look up'])
    expect(byPath(got.rows)).toEqual(byPath((await allRows(PLAIN)).filter(r => got.roots.includes(r.path))))
  })
  it('v1 names groups', async () => {
    const h = await openIndex(env, SEARCH_V1)
    const got = (await find(h, parseQuery('gr')!, '', { ...SEARCH_LIMITS, namesRgs: 1 }))!
    // `grug` is a heavy name (first names group); the rest are not read.
    expect([got.roots, got.truncated, got.stats.namesRgs, got.reason]).toEqual([['bk/runs/grug'], true, 1, 'the name search hit its read budget (1 name group)'])
  })
  it('an unselective trigram is no constraint: same roots, more names verified', async () => {
    for (const date of [SEARCH, SEARCH_V1]) {
      const got = (await find(await openIndex(env, date), parseQuery('safetensors')!, '', { ...SEARCH_LIMITS, triRgs: 0 }))!
      expect([date, got.roots, got.truncated, got.stats.mode]).toEqual([date, await brute('safetensors', ''), false, 'scan'])
    }
  })
})

describe('I/O', () => {
  it('v2 reads a query’s rows groups as merged range reads', async () => {
    // A generation of its own: nothing decoded yet in this isolate.
    const { db, raw } = await sqliteD1('cw')
    const date = '2026-10-01T0101'
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: await readJson<Record<string, D1Variant>>('v2-search/d1.json'), files })
    for (const role of V2_FILES) FILES.set(searchKey(dirOf(date), role), fixture(`v2-search/${SEARCH_FILES[role]}`))
    const e = { ...env, DB: db } as Env
    const h = await openIndex(e, date)
    GETS.length = 0
    const pred = parseQuery('0598')!
    const got = (await searchRoots(e, h, pred, planPositive(pred.ast!)!, ''))!
    expect(got.roots).toEqual(await brute('0598', ''))
    // 9 rows groups (~5 KiB each, all within `RUN_GAP` of the next): one read.
    const rows = searchKey(dirOf(date), 'rows')
    expect([got.stats.rowsRgs, GETS.filter(g => g.key === rows).length]).toEqual([9, 1])
    // No names file, no `path` group.
    expect(GETS.filter(g => g.key.endsWith('/path-index.names.parquet') || g.key.endsWith('/path-index.parquet'))).toEqual([])
  })
  it('a repeated search is answered from the isolate', async () => {
    const h = await openIndex(env, SEARCH)
    const pred = parseQuery('ttl')!
    const plan = planPositive(pred.ast!)!
    const key = `pos:${JSON.stringify(pred.ast)}`
    const a = (await searchRoots(env, h, pred, plan, '', SEARCH_LIMITS, key))!
    GETS.length = 0
    const b = (await searchRoots(env, h, pred, plan, '', SEARCH_LIMITS, key))!
    expect([b === a, GETS]).toEqual([true, []])
    // Another root, other limits: searched afresh.
    const c = (await searchRoots(env, h, pred, plan, 'bk', SEARCH_LIMITS, key))!
    expect([c === a, c.roots]).toEqual([false, a.roots.filter(r => r.startsWith('bk/'))])
  })
  it('a search cut by the wall clock is not kept', async () => {
    const h = await openIndex(env, SEARCH)
    const pred = parseQuery('ckpt')!
    const plan = planPositive(pred.ast!)!
    const key = `pos:${JSON.stringify(pred.ast)}`
    const late = (await searchRoots(env, h, pred, plan, '', { ...SEARCH_LIMITS, wallMs: 0 }, key))!
    const again = (await searchRoots(env, h, pred, plan, '', { ...SEARCH_LIMITS, wallMs: 0 }, key))!
    expect([late.truncated, again === late]).toEqual([true, false])
  })
})

describe('the filter view', () => {
  const base = { w: 1280, h: 800, minArea: 12, atten: 1 }
  it('= the pre-index view when that read sees every row (threshold 0), but the roots come from the index', async () => {
    for (const q of ['ttl', 'safetensors', 'ckpt|swarm', 'ckpt final', 'model-*', '/model-\\d+/', '/^model-\\d+/']) {
      for (const path of ['', 'bk']) {
        const o = { ...base, path, threshold: 0, query: parseQuery(q)! }
        const [a, b] = await Promise.all([buildView(env, { ...o, date: SEARCH }), buildView(env, { ...o, date: PLAIN })])
        // The regex fallback never uses the index (and `^` anchors to the
        // full path: no bucket starts with `model-`).
        const tier = q.startsWith('/') ? b.tier.split('+')[0] : 'search'
        expect([q, path, a.tier.split('+')[0], { ...bare(a), tier: '' }]).toEqual([q, path, tier, { ...bare(b), tier: '' }])
        // The pre-index read (and the regex fallback) can miss small matches,
        // and says so.
        const rx = q.startsWith('/')
        expect([q, path, coverage(a), coverage(b)]).toEqual([q, path, rx ? { approximate: APPROX_UNINDEXED } : {}, { approximate: rx ? APPROX_UNINDEXED : APPROX_NO_INDEX }])
      }
    }
  })

  it('the `regex` syntax end to end: the full-path regex, never from the index', async () => {
    const o = { ...base, path: 'bk', threshold: 0 }
    const v = await buildView(env, { ...o, date: SEARCH, query: parseQuery('^bk/tmp/ttl=\\d+d$', regex)! })
    expect([v.matches, v.matched, v.tree.b, v.tier.split('+')[0]]).toEqual([
      ['bk/tmp/ttl=14d', 'bk/tmp/ttl=7d'],
      [{ path: 'bk/tmp/ttl=14d', b: 5 * MiB, o: 2 }, { path: 'bk/tmp/ttl=7d', b: 2 * MiB, o: 1 }],
      7 * MiB,
      'path',
    ])
    expect(coverage(v)).toEqual({ approximate: APPROX_UNINDEXED })
    // = the `simple` syntax's `/…/` fallback, and the sidecar-less generation.
    for (const q of ['^bk/tmp/ttl=\\d+d$', 'model-\\d+', 'ckpt[^/]*final']) {
      const [a, b, c] = await Promise.all([
        buildView(env, { ...o, date: SEARCH, query: parseQuery(q, regex)! }),
        buildView(env, { ...o, date: SEARCH, query: parseQuery(`/${q}/`)! }),
        buildView(env, { ...o, date: PLAIN, query: parseQuery(q, regex)! }),
      ])
      expect([q, a.matches]).toEqual([q, await brute(`/${q}/`, 'bk')])
      expect([{ ...a, tier: '' }, { ...a, tier: '' }]).toEqual([{ ...b, tier: '' }, { ...c, tier: '' }])
    }
  })

  it('`maxDepth` caps what is drawn, never where matches are found (all matches at depth ≥ 2)', async () => {
    const want = await brute('ttl=', '')
    expect(want).toEqual(['bk/tmp/ttl=14d', 'bk/tmp/ttl=7d'])
    for (const date of [SEARCH, PLAIN]) {
      const v = await buildView(env, { ...base, date, path: '', threshold: 0, maxDepth: 1, query: parseQuery('ttl=')! })
      expect([date, v.matches, v.matched?.map(m => m.b), v.tree.b, coverage(v)]).toEqual([date, want, [5 * MiB, 2 * MiB], 7 * MiB, date === PLAIN ? { approximate: APPROX_NO_INDEX } : {}])
    }
    // The diff's first paint (`depth=1`), with and without sidecars on either side.
    for (const [from, to] of [[PLAIN, SEARCH], [PLAIN, PLAIN]]) {
      const d = await buildDiff(env, { ...base, from, to, top: 100, path: '', threshold: 0, depth: 1, query: parseQuery('ttl=')! })
      expect([from, to, d.matched?.map(m => m.path), d.total_a, d.total_b, d.approximateReason]).toEqual([from, to, want, 7 * MiB, 7 * MiB, APPROX_NO_INDEX])
    }
  })

  it('a search cut before any root falls back to the pre-index read', async () => {
    const o = { ...base, path: '', threshold: 0, query: parseQuery('ttl')! }
    const b = await buildView(env, { ...o, date: PLAIN })
    expect(coverage(b)).toEqual({ approximate: APPROX_NO_INDEX })
    for (const [date, limits, why] of [
      [SEARCH, { rowsRgs: 0 }, 'the row read hit its budget (0 row groups)'],
      [SEARCH_V1, { pathRgs: 0 }, 'the row read hit its budget (0 path groups)'],
      [SEARCH, { wallMs: 0 }, 'the search hit its time budget (0.0 s)'],
    ] as const) {
      const a = await buildView(env, { ...o, date, searchLimits: { ...SEARCH_LIMITS, ...limits } })
      expect([date, why, bare(a), coverage(a), a.tier]).toEqual([date, why, bare(b), { partial: `the search stopped before finding a match (${why}); showing a thresholded read` }, 'path+path'])
    }
  })

  it('finds a match below the pixel budget the pre-index read can’t see', async () => {
    const o = { ...base, path: '', query: parseQuery('notes')! }
    const [a, b] = await Promise.all([buildView(env, { ...o, date: SEARCH }), buildView(env, { ...o, date: PLAIN })])
    expect([a.matches, a.matched, a.tree.b]).toEqual([['bk/iris/notes.txt'], [{ path: 'bk/iris/notes.txt', b: 50, o: 1 }], 50])
    expect([b.matches, b.tree.b]).toEqual([[], 0])
  })

  it('`matched` is heaviest first', async () => {
    const v = await buildView(env, { ...base, date: SEARCH, path: '', query: parseQuery('safetensors|ttl=')! })
    expect(v.matched).toEqual([
      { path: 'bk/models/llama/model-00001-of-00002.safetensors', b: 8 * MiB, o: 1 },
      { path: 'bk/models/llama/model-00002-of-00002.safetensors', b: 8 * MiB, o: 1 },
      { path: 'bk/tmp/ttl=14d', b: 5 * MiB, o: 2 },
      { path: 'bk/tmp/ttl=7d', b: 2 * MiB, o: 1 },
      { path: 'bk/models/tiny.safetensors', b: 10, o: 1 },
    ])
  })

  it('a search cut after finding roots: the view is `partial`, with the reason', async () => {
    const v = await buildView(env, { ...base, date: SEARCH, path: '', threshold: 0, query: parseQuery('0598')!, searchLimits: { ...SEARCH_LIMITS, rowsRgs: 1 } })
    expect([v.matches, v.truncated, v.partial, v.partialReason, v.approximate]).toEqual([
      ['bk/fill/f00598', 'bk/fill/f05987'], true, true, 'the row read hit its budget (1 row group)', undefined,
    ])
    const d = await buildDiff(env, { ...base, from: SEARCH, to: SEARCH_PQ, top: 100, path: '', threshold: 0, query: parseQuery('0598')!, searchLimits: { ...SEARCH_LIMITS, rowsRgs: 1 } })
    expect(coverage(d as unknown as View)).toEqual({ partial: 'the row read hit its budget (1 row group)' })
    const v1 = await buildView(env, { ...base, date: SEARCH_V1, path: '', threshold: 0, query: parseQuery('0598')!, searchLimits: { ...SEARCH_LIMITS, pathRgs: 1 } })
    expect([v1.matches, v1.partialReason]).toEqual([Array.from({ length: 10 }, (_, i) => `bk/fill/f0598${i}`), 'the row read hit its budget (1 path group)'])
  })

  it('a filtered diff uses it on both sides', async () => {
    // The same generation on both sides: one finds its roots by index, the
    // other by the pre-index read; nothing differs.
    const d = await buildDiff(env, { ...base, from: PLAIN, to: SEARCH, top: 100, path: '', threshold: 0, query: parseQuery('ttl')! })
    expect([d.rows, d.tier, d.total_a, d.total_b]).toEqual([[], 'search+path', 7352542, 7352542])
    // The sidecar-less side was read without the index: the diff says so.
    expect([d.partial, d.approximate, d.approximateReason]).toEqual([undefined, true, APPROX_NO_INDEX])
    expect(d.matched?.map(m => m.path)).toEqual(['bk/fill/zz-TTL-b', 'bk/fill/zz-ttl-a', 'bk/iris/TTL-misc', 'bk/tmp/ttl=14d', 'bk/tmp/ttl=7d', 'zz/Checkpoints/ttl'])
  })
})

/** A view tree as `[path, bytes]` rows, depth first (folds as `…/(other)`). */
function flat(n: { n: string; b: number; c?: unknown[] }, at = ''): [string, number][] {
  const p = at === '' ? '' : at
  const out: [string, number][] = [[p, n.b]]
  for (const c of (n.c ?? []) as { n: string; b: number; c?: unknown[] }[]) out.push(...flat(c, p ? `${p}/${c.n}` : c.n))
  return out
}

describe('NOT: excluded descendants leave their roots’ totals and the tree', () => {
  const base = { w: 1280, h: 800, minArea: 12, atten: 1, threshold: 0 }
  it('subtracts the outermost excluded paths: `tmp -ckpt`', async () => {
    const v = await buildView(env, { ...base, date: SEARCH, path: '', query: parseQuery('tmp -ckpt')! })
    // bk/tmp holds 10 MiB; its `run-a/ckpt` (4 MiB, one object) is excluded.
    expect([v.matches, v.matched, v.excluded, v.tree.b, v.tier]).toEqual([['bk/tmp'], [{ path: 'bk/tmp', b: 6 * MiB, o: 3 }], ['bk/tmp/ttl=14d/run-a/ckpt'], 6 * MiB, 'search+path'])
    expect(flat(v.tree)).toEqual([
      ['', 6 * MiB], ['bk', 6 * MiB], ['bk/tmp', 6 * MiB],
      ['bk/tmp/scratch', 3 * MiB], ['bk/tmp/scratch/q.bin', 3 * MiB],
      ['bk/tmp/ttl=7d', 2 * MiB], ['bk/tmp/ttl=7d/z.bin', 2 * MiB],
      ['bk/tmp/ttl=14d', MiB], ['bk/tmp/ttl=14d/run-a', MiB], ['bk/tmp/ttl=14d/run-a/y.bin', MiB],
    ])
  })

  it('only negatives: the view root less the outermost excluded paths', async () => {
    const v = await buildView(env, { ...base, date: SEARCH, path: 'bk', query: parseQuery('-fill -models')! })
    const all = await allRows(PLAIN)
    const size = (p: string) => all.filter(r => r.path === p).reduce((n, r) => n + r.size, 0)
    expect([v.matches, v.excluded, v.tree.b]).toEqual([['bk'], ['bk/fill', 'bk/models'], size('bk') - size('bk/fill') - size('bk/models')])
  })

  it('the index and the pre-index read agree (threshold 0)', async () => {
    const got: unknown[] = []
    const want: unknown[] = []
    for (const q of ['tmp -ckpt', 'ttl -14d', 'ttl -"ttl=14d"', '-fill', 'bk -fill -tmp', 'ckpt -final|safetensors -00002', '-x*bin', 'iris -inner*ttl']) {
      for (const path of ['', 'bk', 'bk/tmp']) {
        const o = { ...base, path, query: parseQuery(q)! }
        const [a, b] = await Promise.all([buildView(env, { ...o, date: SEARCH }), buildView(env, { ...o, date: PLAIN })])
        got.push([q, path, { ...bare(a), tier: '' }, coverage(a), coverage(b)])
        // Without the index: phase 1 is approximate, or (the root matches)
        // only the exclusions are.
        want.push([q, path, { ...bare(b), tier: '' }, {}, { approximate: o.query(path) ? APPROX_EXCL_NO_INDEX : APPROX_NO_INDEX }])
      }
    }
    expect(got).toEqual(want)
  })

  it('an excluded path below the pixel budget is still subtracted — only the index finds it', async () => {
    const all = await allRows(PLAIN)
    const size = (p: string) => all.filter(r => r.path === p).reduce((n, r) => n + r.size, 0)
    const o = { w: 1280, h: 800, minArea: 12, atten: 1, path: '', query: parseQuery('bk -notes')! }
    const [a, b] = await Promise.all([buildView(env, { ...o, date: SEARCH }), buildView(env, { ...o, date: PLAIN })])
    expect([a.excluded, a.tree.b]).toEqual([['bk/iris/notes.txt'], size('bk') - 50])
    expect([b.excluded, b.tree.b]).toEqual([undefined, size('bk')])
    expect([coverage(a), coverage(b)]).toEqual([{}, { approximate: APPROX_NO_INDEX }])
  })

  it('a filtered diff subtracts on both sides', async () => {
    const d = await buildDiff(env, { ...base, from: PLAIN, to: SEARCH, top: 100, path: '', query: parseQuery('tmp -ckpt')! })
    expect([d.rows, d.total_a, d.total_b, d.matched]).toEqual([[], 6 * MiB, 6 * MiB, [{ path: 'bk/tmp', b: 6 * MiB, o: 3 }]])
  })
})
