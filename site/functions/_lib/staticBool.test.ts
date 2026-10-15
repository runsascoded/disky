import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery } from './scope'
import { type BoolAnswer, type BoolBudget, boolPlan, compound, evalBool, isDecline } from './staticBool'
import { drillSource, type HitSource, injectedStores, liveTotal, SuffixHits, type StaticFilterStore } from './staticFilter'
import { type Blobs, StaticNames } from './staticNames'
import { parseKey } from './staticAnchors'
import { makeSimple, parseAst, simple } from './querySyntax'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type View } from './view'
import { searchKey } from './search'
import { type Brute, brute, fixtureObjects, type Objects, objectsSource, SCANS } from './testBrute'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// Compound queries from the static name index (`staticBool.ts`): AND (`a b`), OR (`a|b`), NOT (`a -b`, `-b`), over
// the static-filter fixture's two scans (`fixtures/static-filter/`) and the drill built from them
// (`fixtures/static-drill/`: R = 6 root rows, K = 3 kept children, so heavy directories are everywhere). Every answer
// is checked equal to brute force from the objects (`testBrute.ts`, the fixture's `gen.py` objects): the match roots
// net of exclusions, the excluded paths, the totals — or a decline. Three sources: the suffix index alone (light
// terms), light + drill (a literal over 40 suffix rows asks the drill, as a catalog member does), the drill alone.

const [A, B] = SCANS
const W = 1280, H = 768, MIN_AREA = 12, ATTEN = 2
const dirOf = (date: string) => `cw-l2/${date}/index/g`

/** AND, NOT, both, NOT alone, OR (with AND-groups and NOT). */
const QUERIES = [
  'tomat bin', 'data tomat', 'raw tomat', 'tom ato', 'tomat inner', 'ckpt tomat', 'safetensors models', 'ttl bin', 'tomat x.bin', 'fil f00', 'f00 fill',
  'tomat -bin', 'tomat -inner', 'tomat -a.bin', 'data -tomat', 'ttl -7d', 'tomat -t', 'tomat -bin -inner', 'ckpt -0', 'data -f00', 'bin -tomat', 'fil -f001',
  'data tomat -csv', 'tomat bin -inner', 'raw bin -tomat',
  '-tomat', '-bin', '-om', '-fil', '-f00', '-x.bin -tomat', '-0', '-safetensors',
  'tomat|ttl', 'ckpt|safetensors', 'tomat|bin', 'raw|tomato', 'tomato|tomat', 'data tomat|ckpt', 'tomat|ttl -bin', 'x.bin|tomat -inner', 'f00|tomat', 'nomatch|ckpt', 'tomat|-bin',
]
const ROOTS = ['', 'bk', 'bk/data', 'bk/data/raw', 'bk/data/tomato', 'bk/data/raw/tomat-1', 'zz', 'tomato-bk', 'bk/runs/x', 'bk/fill', 'bk/models']

let base: Env
const objects: Record<string, Objects> = Object.fromEntries(SCANS.map(d => [d, fixtureObjects(d)]))
const filterFiles = new Map<string, ArrayBuffer>()
const drillFiles = new Map<string, ArrayBuffer>()

function blobsOf(files: Map<string, ArrayBuffer>): Blobs {
  const bytes = (k: string) => { const b = files.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
type Kind = 'light' | 'mixed' | 'heavy'
/** The source a store kind reads: the suffix index alone; + the drill past 40 rows; the drill alone. */
function source(kind: Kind): HitSource {
  const names = new StaticNames(blobsOf(filterFiles))
  if (kind === 'light') return new SuffixHits(names)
  return new SuffixHits(names, { maxRows: kind === 'mixed' ? 40 : 0, heavy: drillSource(blobsOf(drillFiles)) })
}
const store = (src: HitSource): StaticFilterStore => ({ source: src, scans: async () => [...SCANS], gen: 'fixture' })
const envOf = (s: StaticFilterStore, extra: Record<string, string> = {}): Env => { const env = { ...base, ...extra } as Env; injectedStores.set(env, s); return env }

async function readTree(dir: string, into: Map<string, ArrayBuffer>, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, into, k)
    else { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); into.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  await readTree('static-drill', drillFiles)
  await readTree('static-filter/sx', filterFiles, 'sx')
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of ['shards.json', 'scans.json']) {
    const b = fs.readFileSync(fixture(`static-filter/${k}`))
    filterFiles.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer)
  }
  const { db, raw } = await sqliteD1('cw')
  for (const date of SCANS) {
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
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

const q = (t: string) => parseQuery(t)!
const plan = (t: string) => boolPlan(parseAst(t, makeSimple({ minTerm: 1 }))!)

describe('boolPlan: the queries the static index answers beyond one literal', () => {
  it('AND-groups of literals, OR between them (looser than AND), the whole query\'s negatives; lowercase keys', () => {
    expect(['tomat bin', 'TOMAT -Bin', 'a b|c', 'a|b c', 'a b|c -d', '-x.bin -tomat', 'abc', 'a|b|c', '-a|b', 'a -b|c'].map(plan)).toEqual([
      { alts: [['tomat', 'bin']], neg: [] },
      { alts: [['tomat']], neg: ['bin'] },
      { alts: [['a', 'b'], ['c']], neg: [] },
      { alts: [['a'], ['b', 'c']], neg: [] },
      { alts: [['a', 'b'], ['c']], neg: ['d'] },
      { alts: [[]], neg: ['x.bin', 'tomat'] },
      { alts: [['abc']], neg: [] },
      { alts: [['a'], ['b'], ['c']], neg: [] },
      // An alternative of negatives only is "everything": every other alternative is inside it.
      { alts: [[]], neg: ['a'] },
      { alts: [['a'], ['c']], neg: ['b'] },
    ])
  })
  it('quotes keep spaces, `-`, `|` literal; a quoted `-x` is a positive term', () => {
    expect(['"a b" -c', '"a|b" c', '"-draft" ckpt', '-"a b" x', '" lead" "x y"|z'].map(plan)).toEqual([
      { alts: [['a b']], neg: ['c'] },
      { alts: [['a|b', 'c']], neg: [] },
      { alts: [['-draft', 'ckpt']], neg: [] },
      { alts: [['x']], neg: ['a b'] },
      { alts: [[' lead', 'x y'], ['z']], neg: [] },
    ])
  })
  it('anchored terms are keys of their own (`/q` starts, `q/` ends, `/q/` is), in any position; escaped anchors are literal', () => {
    expect(['^tomat bin', 'tomat -.pt$', '^ckpt$|.json$', '-^tmp', '\\^a b', '"^a" b'].map(plan)).toEqual([
      { alts: [['/tomat', 'bin']], neg: [] },
      { alts: [['tomat']], neg: ['.pt/'] },
      { alts: [['/ckpt/'], ['.json/']], neg: [] },
      { alts: [[]], neg: ['/tmp'] },
      { alts: [['^a', 'b']], neg: [] },
      { alts: [['^a', 'b']], neg: [] },
    ])
  })
  it('duplicates collapse; an AND-group holding every term of another is inside it and dropped', () => {
    expect(['a a b', 'a|a', 'a|a b', 'a b|a', 'a b|b a', '-a -a x', 'a b|a c|a'].map(plan)).toEqual([
      { alts: [['a', 'b']], neg: [] },
      { alts: [['a']], neg: [] },
      { alts: [['a']], neg: [] },
      { alts: [['a']], neg: [] },
      { alts: [['a', 'b']], neg: [] },
      { alts: [['x']], neg: ['a'] },
      { alts: [['a']], neg: [] },
    ])
  })
  it('not a plan: a glob, a regex, a `/`, an anchor too short for the index — anywhere in the query', () => {
    expect(['tom*at bin', 'abc -/x', '/ttl/', 'a/b|c', 'x -tom*', '^a b', 'gz$ x', 'x -gz$'].map(plan)).toEqual([null, null, null, null, null, null, null, null])
  })
  it('`compound`: beyond one positive literal', () => {
    expect(['abc', 'abc def', '-d', 'abc -d', 'a|b', '^abc', '^ab|cd'].map(s => compound(plan(s)))).toEqual([false, true, true, true, true, false, true])
  })
  it('`is:file` / `is:dir` are refused, never searched as text; quoted they are text', () => {
    expect(['is:file', 'tomat is:file', '-is:dir x', 'IS:FILE', '"is:file"', 'is:files'].map(s => simple.parse(s))).toEqual([
      { error: '“is:file” and “is:dir” aren’t supported yet; quote it ("is:file") to search for the text', code: 'unsupported-kind' },
      { error: '“is:file” and “is:dir” aren’t supported yet; quote it ("is:file") to search for the text', code: 'unsupported-kind' },
      { error: '“is:file” and “is:dir” aren’t supported yet; quote it ("is:file") to search for the text', code: 'unsupported-kind' },
      { error: '“is:file” and “is:dir” aren’t supported yet; quote it ("is:file") to search for the text', code: 'unsupported-kind' },
      { ast: { alts: [[{ kind: 'sub', text: 'is:file' }]], neg: [] } },
      { ast: { alts: [[{ kind: 'sub', text: 'is:files' }]], neg: [] } },
    ])
  })
})

/** Everything under a path (brute force's predicate for "P is the one match root"). */
const all = Object.assign(() => true, { ast: { alts: [[]], neg: [] }, pos: () => true, neg: null })

/** An answer as brute force's shape on `date`: the match roots live on it `[path, bytes, objects]`, net of the
 *  excluded paths under them, the live excluded paths, the total. `whole`: P is the one root, its own total (all
 *  objects under it) less the excluded. */
function asBrute(a: BoolAnswer, P: string, date: string): Brute {
  if (a.none) return { roots: [], excluded: [], total: [0, 0] }
  const live = (hs: typeof a.roots) => {
    const m = new Map<string, [number, number]>()
    for (const h of hs) {
      const t = liveTotal([h], date)
      if (!t.roots) continue
      const e = m.get(h.path) ?? [0, 0]
      m.set(h.path, [e[0] + t.b, e[1] + t.o])
    }
    return m
  }
  const ex = live(a.excl)
  const roots = a.whole ? new Map([[P, brute(objects[date], all, P).total]]) : live(a.roots)
  for (const [e, [b, o]] of ex) {
    let r = e
    while (r.length > P.length && !roots.has(r)) r = r.slice(0, Math.max(0, r.lastIndexOf('/')))
    const t = roots.get(r)!
    roots.set(r, [t[0] - b, t[1] - o])
  }
  const rows = [...roots].map(([p, [b, o]]) => [p, b, o] as [string, number, number]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
  return { roots: rows, excluded: [...ex.keys()].sort(), total: rows.reduce<[number, number]>((t, r) => [t[0] + r[1], t[1] + r[2]], [0, 0]) }
}

/** Every (query, root, date) answer from `src` = brute force; the served / declined counts (per date). */
async function sweepAnswers(src: HitSource, queries = QUERIES, budget?: BoolBudget): Promise<{ served: number; declined: Record<string, number> }> {
  const got: unknown[] = [], want: unknown[] = []
  let served = 0
  const declined: Record<string, number> = {}
  for (const t of queries) for (const root of ROOTS) {
    const a = await evalBool(src, plan(t)!, root, null, budget)
    if (isDecline(a)) { declined[a.declined] = (declined[a.declined] ?? 0) + SCANS.length; continue }
    for (const date of SCANS) {
      served++
      got.push([t, root, date, asBrute(a, root, date)])
      want.push([t, root, date, brute(objects[date], q(t), root)])
    }
  }
  expect(got).toEqual(want)
  return { served, declined }
}

describe('evalBool = brute force from the objects', () => {
  it('the suffix index alone (every literal light): every compound answered', async () => {
    // Declined: the 1–2 character terms (`-0`, `-om`, `-t`, …), which the suffix shards can't hold (no heavy source).
    expect(await sweepAnswers(source('light'))).toEqual({ served: 920, declined: { term: 48 } })
  }, 60_000)
  it('light + drill (literals over 40 suffix rows from the drill: descents through rollups; declines where it can\'t be exact)', async () => {
    // `too-broad`: `f00` (heavy) with `fil` / `fill`, whose roots under `bk/fill` are every `f00…` file, past the
    // rollup there; `-f00`, `-0` under `bk/fill` (a remainder); `f00|tomat` (an alternative that is one heavy
    // term). `term`: `f001`, over 40 rows and no drill member (a real index has it light or a member).
    expect(await sweepAnswers(source('mixed'))).toEqual({ served: 932, declined: { 'too-broad': 30, term: 6 } })
  }, 60_000)
  it('the drill alone (every literal a member)', async () => {
    // `term`: the long literals that are no drill member (`raw`, `inner`, `models`, …: light in a real index).
    expect(await sweepAnswers(source('heavy'))).toEqual({ served: 674, declined: { 'too-broad': 46, term: 248 } })
  }, 60_000)
})

describe('anchored terms in a compound (`^q`, `q$`, `^q$`): the same rules, each term held per name segment', () => {
  // Anchored keys from brute force over the objects (`objectsSource`; the fixture's index has no anchors build),
  // plain ones from the suffix index.
  const mixedAnchors = (): HitSource => {
    const light = source('light'), anchored = objectsSource()
    return { hits: (k, u, o) => (parseKey(k).mode ? anchored : light).hits(k, u, o) }
  }
  const QS = ['^tomat bin', 'tomat -.pt$', '^ckpt|.parquet$', '-^tomat', '.bin$ -^tomat', '^tomat$|x.bin', '^tomat -^tomat$', 'data .bin$|^ckpt -inner', '^raw tomat']
  it('= brute force at every root, both scans', async () => {
    expect(await sweepAnswers(mixedAnchors(), QS)).toEqual({ served: QS.length * ROOTS.length * SCANS.length, declined: {} })
  })
  it('AND roots: the outermost paths holding every term — `^tomat` from a folder, `bin` from the files in it', async () => {
    const a = await evalBool(mixedAnchors(), plan('^tomat bin')!, 'bk/data')
    expect(isDecline(a) ? a : [...new Set(a.roots.map(h => h.path))].sort()).toEqual([
      'bk/data/raw/tomat-1/TOMAT-inner/y.bin', 'bk/data/raw/tomat-1/x.bin', 'bk/data/tomato/a.bin', 'bk/data/tomato/b.bin', 'bk/data/tomato/c.bin',
    ])
  })
})

describe('path terms in a compound (`a/b`): the same rules, each term held along the path', () => {
  // Path keys from brute force over the objects (`objectsSource`; the fixture's index has no name index), plain ones
  // from the suffix index.
  const mixedPaths = (): HitSource => {
    const light = source('light'), byPath = objectsSource()
    return { hits: (k, u, o) => (parseKey(k).mode ? byPath : light).hits(k, u, o) }
  }
  const QS = ['data/tomat', '/tomat', 'bk/data', 'data/tomat bin', 'raw/tomat -inner', 'tomat -data/raw', 'bk/runs|data/raw', '^tomat-1/x.bin$', 'x/ckpt -2.pt', 'tomato/a.bin|ttl=7d/z.', '-tomat-1/to']
  it('= brute force at every root, both scans', async () => {
    expect(await sweepAnswers(mixedPaths(), QS)).toEqual({ served: QS.length * ROOTS.length * SCANS.length, declined: {} })
  })
  it('keys: a path term with a readable tail is a key of its own; a tail too short is no plan', () => {
    expect(['data/tomat bin', 'a/bc -x', 'a/b x', 'x -a/', 'a/b$ x'].map(plan)).toEqual([
      { alts: [['//00data/tomat', 'bin']], neg: [] }, { alts: [['//00a/bc']], neg: ['x'] }, null, null, { alts: [['//01a/b', 'x']], neg: [] },
    ])
  })
})

describe('the budget: a combination past it declines (`too-broad`), never a guess', () => {
  it('the reads past each term\'s own at P, and the hits held, each bound it', async () => {
    const src = source('mixed')
    // `-om` at the fleet root: heavy there, enumerated through the drill's rollups and root reads (`at`).
    const t = '-om'
    const ok = await evalBool(src, plan(t)!, '')
    if (isDecline(ok)) throw new Error(ok.reason)
    const { reads, hits } = ok.io
    expect([reads > 0, hits > 0]).toEqual([true, true])
    expect([
      await evalBool(src, plan(t)!, '', null, { reads: reads - 1, rows: 1e9 }),
      await evalBool(src, plan(t)!, '', null, { reads: 1e9, rows: hits - 1 }),
    ]).toEqual([
      { declined: 'too-broad', reason: `over ${reads - 1} reads` },
      { declined: 'too-broad', reason: `over ${hits - 1} hits` },
    ])
    const again = await evalBool(src, plan(t)!, '', null, { reads, rows: hits })
    expect(isDecline(again) ? again : [again.io.reads, again.io.hits]).toEqual([reads, hits])
  })
})

/** A view as brute force's shape: its match roots `[path, bytes, objects]` (net; a capped list, `matchesCapped`, the
 *  ones it lists) with their count and totals, excluded paths, total. A view the query matches with nothing to
 *  exclude lists no `matched`: it is its own root. */
function viewAsBrute(v: View, root: string): unknown {
  const roots = v.matched ? v.matched.map(m => [m.path, m.b, m.o] as [string, number, number]).sort((x, y) => (x[0] < y[0] ? -1 : 1)) : v.tree.b || v.tree.o ? [[root, v.tree.b, v.tree.o] as [string, number, number]] : []
  return { roots, count: v.matchCount ?? { n: roots.length, b: v.tree.b, o: v.tree.o }, excluded: v.excluded ?? [], total: [v.tree.b, v.tree.o] }
}
/** Brute force as `v` shows it: nothing matched in bytes is an empty view (`readView`'s rule); a capped list keeps
 *  the roots `v` lists. */
function bruteView(b: Brute, v: View): unknown {
  if (b.total[0] <= 0) return { roots: [], count: { n: 0, b: 0, o: 0 }, excluded: [], total: [0, 0] }
  const listed = v.matchesCapped ? new Set(v.matched!.map(m => m.path)) : null
  return { roots: listed ? b.roots.filter(r => listed.has(r[0])) : b.roots, count: { n: b.roots.length, b: b.total[0], o: b.total[1] }, excluded: b.excluded, total: b.total }
}

describe('the map\'s view (`buildView`) of a compound query from the static index = brute force', () => {
  const view = (env: Env, date: string, path: string, t: string) =>
    buildView(env, { date, path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, query: q(t) })
  for (const kind of ['light', 'mixed'] as const) it(`every query, root and scan the ${kind} source answers: roots, excluded paths, totals; never flagged partial or approximate`, async () => {
    const env = envOf(store(source(kind)))
    const got: unknown[] = [], want: unknown[] = []
    for (const t of QUERIES) for (const root of ROOTS) {
      if (isDecline(await evalBool(source(kind), plan(t)!, root))) continue
      for (const date of SCANS) {
        const v = await view(env, date, root, t)
        // A view root the query matches with nothing to exclude is the plain view; with exclusions, its own one root
        // (`root`) less the static answer's excluded paths; else the static forest.
        const plain = q(t)(root) && !q(t).neg
        const b = brute(objects[date], q(t), root)
        got.push([t, root, date, viewAsBrute(v, root), [v.partial, v.approximate, plain ? null : v.tier.split('+')[0]]])
        want.push([t, root, date, bruteView(b, v), [undefined, undefined, plain ? null : b.total[0] <= 0 ? 'none' : q(t)(root) ? 'root' : 'static']])
      }
    }
    expect(got).toEqual(want)
  }, 60_000)
})

describe('the diff (`buildDiff`) of a compound query from the static index: each side\'s matched total = brute force', () => {
  it('every query and root the light source answers, the two scans compared', async () => {
    const env = envOf(store(source('light')))
    const got: unknown[] = [], want: unknown[] = []
    for (const t of QUERIES) for (const root of ROOTS) {
      if (q(t)(root) && !q(t).neg) continue
      if (isDecline(await evalBool(source('light'), plan(t)!, root))) continue
      const d = await buildDiff(env, { from: A, to: B, path: root, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t) })
      const [a, b] = SCANS.map(date => brute(objects[date], q(t), root).total)
      const shown = (x: [number, number]) => x[0] <= 0 ? [0, 0] : x
      got.push([t, root, d.total_a, d.total_b, d.objects_a, d.objects_b, d.partial, d.approximate])
      want.push([t, root, shown(a)[0], shown(b)[0], shown(a)[1], shown(b)[1], undefined, undefined])
    }
    expect(got).toEqual(want)
  }, 60_000)
})
