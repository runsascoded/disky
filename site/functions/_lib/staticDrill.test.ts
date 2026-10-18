import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery } from './scope'
import { Drill, type DrillAnswer, rollupAt, rollupTotal } from './staticDrill'
import { covers, drillSource, type Found, type HitSource, injectedStores, liveTotal, SuffixHits, type StaticFilterStore } from './staticFilter'
import { catalogAnswer, StaticCatalog } from './staticCatalog'
import { FilterRejected, REJECT_MESSAGES } from './indexedOnly'
import { type Blobs, type Hit, scanAt, StaticNames } from './staticNames'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type Diff, type DiffRow, NotFound, type View, type ViewNode } from './view'
import { searchKey } from './search'
import { resetUpgrades } from './edgeCache'
import { warmSubtree } from '../api/subtree'

// Every test in this file: tests take seconds alone but past 30 s under the full parallel suite (gcs, cw: 10-17).
vi.setConfig({ testTimeout: 120_000 })

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The heavy terms' drilldown (`staticDrill.ts`) over `fixtures/static-drill/` (`gen.py`): the `drill/` files
// and base catalog `dt_cloud.static_roots` builds from the two scans of `fixtures/static-filter/` (R = 6 root
// rows, K = 3 kept children, 4-row data groups, 3-entry index row groups; long members over two shard
// files, aliases; short members), checked against brute force from the objects (`expected.json`) — the
// reader alone, then the map's views served from it (the static-filter fixture's store generations).

describe('D1', () => {
  const A = '2026-10-04'
  const B = '2026-10-05'
  const DATES = [A, B]
  const LONG = ['tomat', 'tomato', 'ttl', 'ckpt', 'ckpt-', 'safetensor', 'safetensors', 'afetensors', 'bin', '.bin', 'x.bin', 'f00', 'fil', 'data', 'nomatch']
  const SHORT = ['0', '.', 'f', 'a', 'x', 'om', 't', 'qq']
  const TERMS = [...LONG, ...SHORT]
  const W = 1280, H = 768, MIN_AREA = 12, ATTEN = 2
  const dirOf = (date: string) => `cw-l2/${date}/index/g`

  type Sums = Record<string, [number, number]>
  type Expected = {
    paths: string[]
    aliases: Record<string, string>
    views: Record<string, Record<string, Record<string, Sums | null>>>
    roots: Record<string, Record<string, Record<string, [string, number, number][] | null>>>
  }
  let expected: Expected
  let base: Env
  const drillFiles = new Map<string, ArrayBuffer>()
  const filterFiles = new Map<string, ArrayBuffer>()

  /** `Blobs` over held files; `reads` collects every key read. */
  function blobsOf(files: Map<string, ArrayBuffer>, reads?: string[], override: Record<string, unknown> = {}): Blobs {
    const bytes = (k: string) => { reads?.push(k); const b = files.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
    return {
      async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
      async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
      async json(key) { return key in override ? override[key] : JSON.parse(new TextDecoder().decode(bytes(key))) },
    }
  }
  const drillBlobs = (reads?: string[], override?: Record<string, unknown>) => blobsOf(drillFiles, reads, override)
  /** The drill alone (keys under `drill/`), with the base catalog for the fleet root. */
  const newDrill = (reads?: string[]) => (drillSource(drillBlobs(reads)) as unknown as { drill: Drill }).drill

  /** A static store over the static-filter fixture's suffix index, every ≥ 3-character literal over `maxRows`
   *  (0) so it asks the drilldown, as a catalog member does. */
  function heavyStore(heavy: HitSource | null = drillSource(drillBlobs())): StaticFilterStore {
    return { source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 0, heavy }), scans: async () => [...DATES], gen: 'fixture' }
  }
  const envStatic = (s: StaticFilterStore = heavyStore()): Env => { const env = { ...base }; injectedStores.set(env, s); return env }

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
    const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
    for (const k of ['shards.json', 'scans.json', 'sx/s0000.parquet', 'sx/s0001.parquet']) {
      const b = fs.readFileSync(fixture(`static-filter/${k}`))
      filterFiles.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer)
    }
    expected = await readJson<Expected>('static-drill/expected.json')
    const { db, raw } = await sqliteD1()
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
    base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', SEARCH_SIDECARS: '1' } as Env
  })

  const n = (x: bigint) => Number(x)
  /** Live roots summed per child of `P` on `date` (zeros dropped, by name). */
  function childSums(hits: Hit[], P: string, date: string): Sums {
    const D = scanAt(date), acc = new Map<string, [number, number]>()
    for (const h of hits) {
      if (!(h.vf <= D && D < h.vt)) continue
      const c = (P === '' ? h.path : h.path.slice(P.length + 1)).split('/')[0]
      const e = acc.get(c) ?? [0, 0]
      acc.set(c, [e[0] + n(h.size), e[1] + n(h.n)])
    }
    return Object.fromEntries([...acc].filter(([, [b, o]]) => b || o).sort(([x], [y]) => (x < y ? -1 : 1)))
  }
  const total = (s: Sums): [number, number] => Object.values(s).reduce<[number, number]>((t, [b, o]) => [t[0] + b, t[1] + o], [0, 0])
  const keptOf = (a: DrillAnswer) => 'rollup' in a ? [...new Set(a.rollup.cells.filter(c => c.kind === 1).map(c => c.child))] : []

  describe('Drill.view: the reader = brute force from the objects', () => {
    it('every term at every path on both scans: roots reads child for child, rollups kept children + remainder', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      const sources: Record<string, number> = {}
      for (const t of TERMS) for (const P of expected.paths) {
        const a = await drill.view(t, P)
        sources[a.source] = (sources[a.source] ?? 0) + 1
        for (const d of DATES) {
          const exp = expected.views[t][P][d]
          if (a.source === 'plain' || a.source === 'none') {
            got.push([t, P, d, a.source])
            want.push([t, P, d, exp === null ? 'plain' : Object.keys(exp).length ? 'members only' : 'none'])
          } else if (a.source === 'roots') {
            got.push([t, P, d, childSums(a.hits, P, d)])
            want.push([t, P, d, exp])
          } else {
            const { kids, rest } = rollupAt(a.rollup, d)
            const kept = keptOf(a)
            got.push([t, P, d, Object.fromEntries(kids.map(([c, b, o]) => [c, [n(b), n(o)]])), [n(rest[0]), n(rest[1])]])
            const keptExp = Object.fromEntries(Object.entries(exp!).filter(([c]) => kept.includes(c)))
            const all = total(exp!), k = total(keptExp)
            want.push([t, P, d, keptExp, [all[0] - k[0], all[1] - k[1]]])
          }
        }
      }
      expect(got).toEqual(want)
      // Every kind of answer occurs: 23 terms × 24 paths.
      expect(sources).toEqual({ plain: 58, none: 24, roots: 457, rollup: 6, catalog: 7 })
    })

    it('the dispatch: roots when the index bounds the range at ≤ R + 2 groups; else the true count is over R (a rollup)', async () => {
      const drill = newDrill()
      const meta = await drill.info()
      const bad: unknown[] = []
      for (const t of TERMS) for (const P of expected.paths) {
        const a = await drill.view(t, P)
        if (a.source === 'roots' && !(a.upper <= meta.dispatch_rows && a.hits.length <= a.upper)) bad.push([t, P, a.upper, a.hits.length])
        if (a.source === 'rollup' && !(a.upper > meta.dispatch_rows && a.rollup.rows! > meta.R && a.rollup.kept <= meta.K)) bad.push([t, P, a.upper, a.rollup.rows])
      }
      expect([meta.R, meta.K, meta.dispatch_rows, bad]).toEqual([6, 3, 14, []])
      const at = async (t: string, P: string) => { const a = await drill.view(t, P); return [t, P, a.source, 'rollup' in a ? [a.rollup.kept, a.rollup.children, a.rollup.rows] : a.source === 'roots' ? a.hits.length : null] }
      expect(await Promise.all([['0', 'bk'], ['0', 'bk/fill'], ['f00', 'bk/fill'], ['0', ''], ['tomat', 'bk'], ['tomat', ''], ['bin', 'bk/data'], ['.', 'bk']].map(([t, P]) => at(t, P)))).toEqual([
        ['0', 'bk', 'rollup', [1, 1, 3000]],
        ['0', 'bk/fill', 'rollup', [3, 3000, 3000]],
        ['f00', 'bk/fill', 'rollup', [3, 1000, 1000]],
        ['0', '', 'catalog', [1, 1, 3000]],
        ['tomat', 'bk', 'roots', 10],
        ['tomat', '', 'catalog', [3, 3, 13]],
        ['bin', 'bk/data', 'roots', 6],
        ['.', 'bk', 'rollup', [3, 5, 16]],
      ])
    })

    it('aliases: a member with another\'s root set reads the canonical\'s rows', async () => {
      const drill = newDrill()
      const al = await drill.aliases()
      expect(Object.fromEntries([...al].filter(([q, a]) => q !== a.c).map(([q, a]) => [q, a.c]))).toEqual(expected.aliases)
      const reads: string[] = []
      const a = await newDrill(reads).view('safetensors', 'bk')
      expect([a.source, 'c' in a ? a.c : null]).toEqual(['roots', 'afetensors'])
    })

    it('one or two characters read the short set, three or more the long', async () => {
      const files = async (t: string, P: string) => { const reads: string[] = []; await newDrill(reads).view(t, P); return [...new Set(reads.filter(k => !k.endsWith('.json')).map(k => k.replace(/^drill\//, '').split(/[-/]/)[0]))].sort() }
      expect([await files('0', 'bk/fill'), await files('om', 'bk'), await files('f00', 'bk/fill'), await files('tomat', 'bk')])
        .toEqual([['short'], ['short'], ['aliases.parquet', 'long'], ['aliases.parquet', 'long']])
    })

    it('inside: a path whose lowercase name holds the literal is the plain view; a long non-member and a missing short literal', async () => {
      const drill = newDrill()
      const got = await Promise.all([['tomat', 'bk/data/tomato'], ['om', 'tomato-bk/a'], ['TOMAT'.toLowerCase(), 'zz/Checkpoints/TOMAT'], ['nomatch', 'bk'], ['qq', 'bk']].map(async ([t, P]) => {
        const a = await drill.view(t, P)
        return [t, P, a.source, a.source === 'roots' ? a.hits.length : null]
      }))
      expect(got).toEqual([
        ['tomat', 'bk/data/tomato', 'plain', null],
        ['om', 'tomato-bk/a', 'plain', null],
        ['tomat', 'zz/Checkpoints/TOMAT', 'plain', null],
        ['nomatch', 'bk', 'none', null],
        ['qq', 'bk', 'roots', 0],
      ])
    })

    it('the series: a roots answer\'s live totals and a rollup\'s running sums = each scan\'s brute-force total', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) for (const P of expected.paths) {
        const a = await drill.view(t, P)
        if (a.source === 'plain' || a.source === 'none') continue
        got.push([t, P, DATES.map(d => { const x = a.source === 'roots' ? liveTotal(a.hits, d) : rollupTotal(a.rollup, d); return [x.b, x.o] })])
        want.push([t, P, DATES.map(d => total(expected.views[t][P][d]!))])
      }
      expect(got).toEqual(want)
    })
  })

  describe('DrillSource: the filter\'s heavy source', () => {
    it('answers with the base scans; plain views and non-members decline', async () => {
      const src = drillSource(drillBlobs())
      const sum = (f: Found | null) => f == null ? null : [f.rollup ? 'rollup' : 'hits', f.scans]
      expect([sum(await src.hits('0', 'bk/fill')), sum(await src.hits('tomat', 'bk')), sum(await src.hits('tomat', 'bk/data/tomato')), sum(await src.hits('nomatch', ''))])
        .toEqual([['rollup', DATES], ['hits', DATES], null, null])
    })

    it('a scan past the drill base (the daily runs\' dates) is not covered: `covers` declines it', async () => {
      const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
      const f = (await src.hits('0', 'bk/fill'))!
      expect([covers(f, [A]), covers(f, [B]), covers(f, [A, B]), covers({ hits: [], io: {} }, [B])]).toEqual([true, false, false, true])
    })
  })

  describe('DrillSource: roots answers across isolates (the colo cache)', () => {
    it('a fresh isolate takes a roots answer from the cache, reading no drill file; rollups are not cached', async () => {
      const held = new Map<string, string>()
      const colo = {
        async match(url: string) { const b = held.get(url); return b == null ? undefined : new Response(b) },
        async put(url: string, r: Response) { held.set(url, await r.text()) },
      } as unknown as Cache
      const first: string[] = []
      const a = (await drillSource(drillBlobs(first), { cache: colo, prefix: 'g' }).hits('tomat', 'bk'))!
      const later: string[] = []
      const b = (await drillSource(drillBlobs(later), { cache: colo, prefix: 'g' }).hits('tomat', 'bk'))!
      const key = (h: Hit) => `${h.path} ${h.depth} ${h.usr} ${h.vf} ${h.vt} ${h.size} ${h.n}`
      // The colo also holds the drill's index tops and aliases (their own keys).
      const answers = () => [...held.keys()].filter(u => u.includes('/roots-v1/')).map(u => decodeURIComponent(u.split('/').pop()!))
      expect([answers(), first.some(k => k.includes('roots')), b.hits!.map(key), later.filter(k => k.includes('roots')), b.scans])
        .toEqual([['tomat\0bk.json'], true, a.hits!.map(key), [], DATES])
      await drillSource(drillBlobs(), { cache: colo, prefix: 'g' }).hits('0', 'bk/fill')
      expect(answers()).toEqual(['tomat\0bk.json'])
    })
  })

  // --- the map's views ------------------------------------------------------------------------------

  const q = (t: string) => parseQuery(t)!
  const view = (env: Env, date: string, path: string, t: string, extra: Partial<Parameters<typeof buildView>[1]> = {}) =>
    buildView(env, { date, path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, query: q(t), ...extra })
  /** The tree's top level: per child `[name, bytes, objects, m?]` (`(other)` included). */
  const top = (v: View) => (v.tree.c ?? []).map(c => [c.n, c.b, c.o, ...(c.m ? ['m'] : [])])
  /** A tree whose synthesized nodes — `(other)` folds and the ancestors holding only match roots — carry no
   *  written-time / read-day / class mix: a static view looks up only its drawn roots' own rows, so an
   *  ancestor of roots under the pixel budget (`bin` at the root: 12 roots, 4 drawn) has bytes, objects and
   *  owners, not those (the search view knows every root's row). Match roots and their insides keep them. */
  const sansFoldDetail = (n: ViewNode, inRoot = false): ViewNode => {
    const { d: _d, a: _a, cb: _cb, ...rest } = n
    const keep = inRoot || !!n.m
    return { ...(keep ? n : rest), ...(n.c ? { c: n.c.map(c => sansFoldDetail(c, keep && c.n !== '(other)')) } : {}) } as ViewNode
  }
  const sansP1 = (v: View) => ({ ...v, tier: v.tier.replace(/^(drill|search)\+/, '+'), tree: sansFoldDetail(v.tree) })
  const matchedRows = (v: { matched?: { path: string; b: number; o: number }[] }) =>
    (v.matched ?? []).map(m => [m.path, m.b, m.o]).sort((x, y) => (x[0] < y[0] ? -1 : 1))

  describe('the map\'s views from the drilldown (`/api/subtree`)', () => {
    it('roots answers: the same view as the search sidecars\', and brute force\'s match roots', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      let n = 0
      for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
        const a = await drill.view(t, P)
        if (a.source !== 'roots' || !a.hits.length) continue
        for (const d of DATES) {
          const roots = expected.roots[t][P][d]
          if (!roots?.length) continue
          const [s, r] = await Promise.all([view(envStatic(), d, P, t), view(base, d, P, t)])
          n++
          got.push([t, P, d, s.tier.split('+')[0], sansP1(s), matchedRows(s)])
          want.push([t, P, d, 'drill', sansP1(r), roots])
        }
      }
      expect(got).toEqual(want)
      expect(n).toBe(256)
    })

    it('rollup answers, one level (`depth=1`): a cell per kept child (match roots marked), the rest in `(other)`; the matched totals exact', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
        const a = await drill.view(t, P)
        if (!('rollup' in a)) continue
        for (const d of DATES) {
          const exp = expected.views[t][P][d]!
          // Every child drawn (a 1-byte floor): the kept ones by name, the remainder as `(other)`.
          const v = await view(envStatic(), d, P, t, { threshold: 0.5, maxDepth: 1 })
          const kept = keptOf(a)
          const keptExp = Object.entries(exp).filter(([c]) => kept.includes(c))
          const all = total(exp), k = total(Object.fromEntries(keptExp))
          const rest = [all[0] - k[0], all[1] - k[1]]
          const isRoot = (c: string) => c.toLowerCase().includes(t)
          got.push([t, P, d, v.tier, v.tree.b, v.tree.o, top(v), matchedRows(v), v.matchCount, v.matchesCapped, v.rollup])
          want.push([t, P, d, 'rollup', all[0], all[1],
            [...keptExp.map(([c, [b, o]]) => [c, b, o, ...(isRoot(c) ? ['m'] : [])]).sort((x, y) => (y[1] as number) - (x[1] as number)), ...(rest[0] > 0.5 ? [['(other)', rest[0], rest[1]]] : [])],
            keptExp.filter(([c]) => isRoot(c)).map(([c, [b, o]]) => [P === '' ? c : `${P}/${c}`, b, o]).sort((x, y) => ((x[0] as string) < (y[0] as string) ? -1 : 1)),
            { n: a.rollup.rows ?? keptExp.filter(([c]) => isRoot(c)).length, b: all[0], o: all[1] }, true, { children: a.rollup.children, kept: a.rollup.kept, rows: a.rollup.rows }])
        }
      }
      expect(got).toEqual(want)
      expect(got.length).toBe(26)
    })

    it('a kept child that is a match root carries its own row\'s kind (a file under `bk/fill` for `0`: the K = 3 largest peaks, ties by name)', async () => {
      const v = await view(envStatic(), A, 'bk/fill', '0', { threshold: 0.5 })
      expect(v.tree.c!.map(c => [c.n, c.k, c.b, c.o, c.m, c.f])).toEqual([
        ['f00011', 'file', 2048, 1, 1, undefined], ['f00023', 'file', 2048, 1, 1, undefined], ['f00035', 'file', 2048, 1, 1, undefined],
        // 3000 files of 1 << (i % 12) bytes: 250 × 4095.
        ['(other)', 'dir', 250 * 4095 - 3 * 2048, 2997, undefined, 2997],
      ])
    })

    it('a rollup under an owner pool reads as before; a heavy literal on a scan past the drill base is refused', async () => {
      const asked: string[] = []
      const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
      const spy: HitSource = { async hits(k, u) { asked.push(`${k}@${u}`); return src.hits(k, u) } }
      const env = envStatic(heavyStore(spy))
      const [onA, onB, pool] = await Promise.all([view(env, A, 'bk/fill', '0'), view(env, B, 'bk/fill', '0').catch((e: FilterRejected) => e.reject.code), view(env, A, 'bk/fill', '0', { owner: 'unowned' })])
      expect([onA.tier, onB, pool.tier.split('+')[0], asked]).toEqual(['rollup', 'scan-not-indexed', 'search', ['0@bk/fill', '0@bk/fill', '0@bk/fill']])
      const rPool = await view(base, A, 'bk/fill', '0', { owner: 'unowned' })
      expect(sansP1(pool)).toEqual(sansP1(rPool))
    })
  })

  describe('a heavy literal on a scan its drilldown doesn\'t cover (a run with no live `drill/` cuts the stack)', () => {
    /** The drill covering `A` only (B past it, as cw's scans past a merged run built without its `drill/`). */
    const cutEnv = () => envStatic(heavyStore(drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))))
    const outcome = async (p: Promise<View>) => {
      try {
        const v = await p
        return [v.tier.split('+')[0], v.matchCount ?? null, v.approximate ?? null]
      } catch (e) {
        if (e instanceof FilterRejected) return ['refused', e.reject.code]
        throw e
      }
    }

    it('`scan-not-indexed` at every depth and under an owner — never the walk\'s "no matches"; the covered scan from the drill', async () => {
      const env = cutEnv()
      const exp = expected.views['0']['bk/fill']
      expect([
        await outcome(view(env, B, 'bk/fill', '0')),
        await outcome(view(env, B, 'bk', '0')),
        await outcome(view(env, B, '', '0')),
        await outcome(view(env, B, 'bk/fill', '0', { owner: 'unowned' })),
        await outcome(view(env, A, 'bk/fill', '0')),
      ]).toEqual([
        ['refused', 'scan-not-indexed'],
        ['refused', 'scan-not-indexed'],
        ['refused', 'scan-not-indexed'],
        ['refused', 'scan-not-indexed'],
        ['rollup', { n: 3000, b: total(exp[A]!)[0], o: total(exp[A]!)[1] }, null],
      ])
    })

    it('a member the drill\'s live tiers don\'t know (its heavy source declines): refused, not walked', async () => {
      const none: HitSource = { hits: async () => null }
      expect(await outcome(view(envStatic(heavyStore(none)), B, 'bk/fill', '0'))).toEqual(['refused', 'scan-not-indexed'])
    })
  })

  describe('the diff from the drilldown (`/api/diff`)', () => {
    it('a rollup, one level (`depth=1`): each kept child\'s two scans\' totals, the remainder\'s Δ in `(other)`', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
        const a = await drill.view(t, P)
        if (!('rollup' in a)) continue
        const d = await buildDiff(envStatic(), { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t), threshold: 0.5, depth: 1 })
        const ea = expected.views[t][P][A]!, eb = expected.views[t][P][B]!
        const kept = keptOf(a)
        const named = kept.filter(c => ea[c] || eb[c]).sort()
        const ka = total(Object.fromEntries(named.map(c => [c, ea[c] ?? [0, 0]]))), kb = total(Object.fromEntries(named.map(c => [c, eb[c] ?? [0, 0]])))
        const ta = total(ea), tb = total(eb)
        const st = (x: [number, number] | undefined, y: [number, number] | undefined): DiffRow['s'] => !x ? 'added' : !y ? 'removed' : x[0] !== y[0] || x[1] !== y[1] ? 'changed' : 'unchanged'
        const rows: unknown[] = named.map(c => [c, st(ea[c], eb[c]), ea[c]?.[0] ?? 0, eb[c]?.[0] ?? 0, ea[c]?.[1] ?? 0, eb[c]?.[1] ?? 0])
        const ra: [number, number] = [ta[0] - ka[0], ta[1] - ka[1]], rb: [number, number] = [tb[0] - kb[0], tb[1] - kb[1]]
        if (ra[0] > 0 || rb[0] > 0) rows.push(['(other)', st(ra[0] > 0 ? ra : undefined, rb[0] > 0 ? rb : undefined), ra[0], rb[0], ra[0] > 0 ? ra[1] : 0, rb[0] > 0 ? rb[1] : 0])
        const sortRows = (rs: unknown[]) => (rs as [string][]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
        got.push([t, P, d.total_a, d.total_b, d.objects_a, d.objects_b, d.lookups, sortRows(d.rows.filter(r => r.s !== 'unchanged').map(r => [r.p, r.s, r.a, r.b, r.oa, r.ob]))])
        want.push([t, P, ta[0], tb[0], ta[1], tb[1], 0, sortRows(rows.filter(r => (r as unknown[])[1] !== 'unchanged'))])
      }
      expect(got).toEqual(want)
      expect(got.length).toBe(13)
    })

    it('a roots answer: the same diff as the search sidecars\'', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of ['tomat', 'bin', 'x', 'om', 'ckpt-', 'safetensors']) for (const P of ['', 'bk', 'bk/data', 'zz']) {
        const a = await drill.view(t, P)
        if (a.source !== 'roots') continue
        for (const extra of [{}, { depth: 1 }, { summary: true }]) {
          const o = { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q(t), ...extra }
          const [s, r] = await Promise.all([buildDiff(envStatic(), o), buildDiff(base, o)])
          got.push([t, P, extra, { ...s, tier: s.tier.replace(/^(drill|search)\+/, '+') }])
          want.push([t, P, extra, { ...r, tier: r.tier.replace(/^(drill|search)\+/, '+') }])
        }
      }
      expect(got).toEqual(want)
      expect(got.length).toBe(66)
    })
  })

  describe('the fleet root\'s root count (`matchCount.n` of a catalog view)', () => {
    it('a short literal\'s = its roots read whole (the count reads only the index\'s edges); a long one\'s = its alias entry', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) {
        const a = await drill.view(t, '')
        if (a.source !== 'catalog') continue
        const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
        const lo: [string, string] = [a.c, ''], hi: [string, string] = [a.c + '\0', '']
        const roots = (await drill.state()).tiers[0].files[a.kind].roots
        const sel = await roots.select(lo, hi, io)
        const all = await roots.read(lo, hi, sel.groups!, io, () => 1)
        got.push([t, a.rollup.rows])
        want.push([t, all.length])
      }
      expect(got).toEqual(want)
      expect(got.map(x => (x as [string, number])[0])).toEqual(['tomat', 'f00', '0', '.', 'a', 'om', 't'])
    })

    it('the count reads only the data groups straddling the range\'s edges, each on its own (not the span between)', async () => {
      const roots = (await newDrill().state()).tiers[0].files.short.roots
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of ['0', '.']) {
        const lo: [string, string] = [t, ''], hi: [string, string] = [t + '\0', '']
        const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
        await roots.count(lo, hi, io)
        const all = (await roots.select(lo, hi, { ...io })).groups!
        const edges = [all[0], all[all.length - 1]].filter(e => e.qMin < t || e.qMax > t || e.qMax === t + '\0')
        got.push([t, io.groups, io.bytes])
        want.push([t, edges.length, edges.reduce((s, e) => s + e.length, 0)])
      }
      expect(got).toEqual(want)
    })
  })

  describe('no drilldown (`FILTER_STATIC_HEAVY` off): a heavy literal never reads as the approximate walk\'s "no matches"', () => {
    /** The static store without a heavy source: the suffix index (literals over `maxRows` suffix rows are heavy)
     *  and, for a heavy literal's fleet root, the base catalog — as `staticFilterStore` wires it. */
    const noDrill = (maxRows = 0): StaticFilterStore =>
      ({ source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows, heavy: null, catalog: new StaticCatalog(drillBlobs()) }), scans: async () => [...DATES], gen: 'fixture' })
    /** Every catalog member (the literals the drill answers from the catalog at the fleet root). */
    const MEMBERS = ['tomat', 'f00', '0', '.', 'a', 'om', 't']
    /** A view's refusal, else its tier ('not found': a path the scan lacks). */
    const outcome = async (p: Promise<View>) => p.then(v => v.tier, e => e instanceof FilterRejected ? e.reject : e instanceof NotFound ? 'not found' : Promise.reject(e))

    it('the fleet root: bucket tiles and the match count exactly the catalog\'s cells (`/api/name-summary`\'s numbers) = brute force', async () => {
      const catalog = new StaticCatalog(drillBlobs())
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of MEMBERS) for (const d of DATES) {
        const v = await view(envStatic(noDrill()), d, '', t, { threshold: 0.5 })
        got.push([t, d, v.tier, v.tree.b, v.tree.o, top(v), matchedRows(v), v.matchCount, v.matchesCapped, v.rollup, v.approximate, v.partial])
        const { member } = await catalog.lookup(t)
        const cells = Object.entries(catalogAnswer(member!, [d])[d]).map(([c, [b, o]]) => [c, Number(b), Number(o)] as [string, number, number])
        // The same cells as brute force from the objects.
        expect([t, d, Object.fromEntries(cells.map(([c, b, o]) => [c, [b, o]]))]).toEqual([t, d, expected.views[t][''][d]])
        const [b, o] = total(expected.views[t][''][d]!)
        const roots = cells.filter(([c]) => c.toLowerCase().includes(t))
        const buckets = new Set(member!.cells.map(c => c.bucket)).size
        want.push([t, d, 'rollup', b, o,
          cells.map(([c, cb, co]) => [c, cb, co, ...(c.toLowerCase().includes(t) ? ['m'] : [])]).sort((x, y) => (y[1] as number) - (x[1] as number)),
          roots.sort((x, y) => (x[0] < y[0] ? -1 : 1)),
          { n: roots.length, b, o }, true, { children: buckets, kept: buckets, rows: null, bucketsOnly: true }, undefined, undefined])
      }
      expect(got).toEqual(want)
      // Spelled out for one: `tomat` on B — three buckets, `tomato-bk` itself a match root.
      const v = await view(envStatic(noDrill()), B, '', 'tomat', { threshold: 0.5 })
      expect([top(v), v.matchCount, v.rollup]).toEqual([
        [['bk', 20127, 8], ['tomato-bk', 110, 2, 'm'], ['zz', 10, 1]],
        { n: 1, b: 20247, o: 11 },
        { children: 3, kept: 3, rows: null, bucketsOnly: true },
      ])
    })

    it('a one- or two-character literal no name holds: exactly zero at the root (no walk, no approximate flag)', async () => {
      const v = await view(envStatic(noDrill()), A, '', 'qq')
      expect([v.tier, v.tree.b, v.tree.o, v.matched, v.approximate]).toEqual(['none', 0, 0, [], undefined])
    })

    it('below the root: `term-too-common`, never the walk — at every heavy path, both scans', async () => {
      const drill = newDrill()
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of MEMBERS) for (const P of expected.paths.filter(p => p !== '' && p !== 'nope')) {
        const a = await drill.view(t, P)
        if (a.source === 'plain' || a.source === 'none') continue
        for (const d of DATES) {
          const missing = await outcome(view(base, d, P, t)) === 'not found'
          got.push([t, P, d, await outcome(view(envStatic(noDrill()), d, P, t))])
          want.push([t, P, d, missing ? 'not found' : { code: 'term-too-common', message: REJECT_MESSAGES['term-too-common'] }])
        }
      }
      expect(got).toEqual(want)
      expect([got.length, got.filter(g => (g as unknown[])[3] === 'not found').length]).toEqual([222, 9])
    })

    it('the root under an owner pool (the catalog knows no owners): `owner-too-common`, not the walk', async () => {
      expect(await outcome(view(envStatic(noDrill()), A, '', '0', { owner: 'unowned' }))).toEqual({ code: 'owner-too-common', message: REJECT_MESSAGES['owner-too-common'] })
    })

    it('a view the literal matches is the plain view, as before', async () => {
      expect(await outcome(view(envStatic(noDrill()), A, 'tomato-bk', 'tomat'))).toBe((await view(base, A, 'tomato-bk', 'tomat')).tier)
    })

    it('light literals (within the suffix bound) are unaffected: the same views as the search sidecars\'', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of ['tomat', 'ckpt', 'bin', 'ttl']) for (const P of ['', 'bk', 'bk/data']) for (const d of DATES) {
        const [s, r] = await Promise.all([view(envStatic(noDrill(1e9)), d, P, t), view(base, d, P, t)])
        got.push([t, P, d, s.tier.split('+')[0], sansP1({ ...s, tier: '' }), matchedRows(s)])
        want.push([t, P, d, r.matched?.length ? 'static' : 'none', sansP1({ ...r, tier: '' }), matchedRows(r)])
      }
      expect(got).toEqual(want)
    })

    it('with the drilldown (`FILTER_STATIC_HEAVY=1`), unchanged: the root from the catalog with its root count, rollups below', async () => {
      const at = async (P: string) => { const v = await view(envStatic(), A, P, '0', { threshold: 0.5 }); return [P, v.tier, v.matchCount, v.rollup] }
      expect(await Promise.all(['', 'bk', 'bk/fill'].map(at))).toEqual([
        ['', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 1, kept: 1, rows: 3000 }],
        ['bk', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 1, kept: 1, rows: 3000 }],
        ['bk/fill', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 3000, kept: 3, rows: 3000 }],
      ])
    })
  })

  // --- nested rollup views: brute force from the objects --------------------------------------------

  /** Each scan's paths from its store generation's `path` sort, every owner slice summed: `[bytes, objects]`. */
  const scanRows = new Map<string, Map<string, [number, number]>>()
  async function loadScanRows(): Promise<void> {
    if (scanRows.size) return
    const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(p: string): Uint8Array }
    const { parquetReadObjects } = await import('hyparquet')
    const { compressors } = await import('./zstd')
    for (const d of DATES) {
      const b = fs.readFileSync(fixture(`static-filter/${d}/path-index.parquet`))
      const rows = await parquetReadObjects({ file: b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer, columns: ['path', 'size', 'n_files'], compressors }) as { path: string; size: bigint | number; n_files: bigint | number }[]
      const m = new Map<string, [number, number]>()
      for (const r of rows) { const e = m.get(r.path) ?? [0, 0]; m.set(r.path, [e[0] + Number(r.size), e[1] + Number(r.n_files)]) }
      scanRows.set(d, m)
    }
  }
  const parent = (p: string) => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : ''
  const depthOf = (p: string) => p === '' ? 0 : p.split('/').length

  /** `t`'s matched `[bytes, objects]` at every path strictly under `P` on `date` (Σ match roots at or under it, the
   *  outermost paths under P whose name holds the literal), and the roots. Brute force from the objects. */
  function matchedUnder(t: string, P: string, date: string): { m: Map<string, [number, number]>; roots: Set<string> } {
    const qp = q(t), m = new Map<string, [number, number]>(), roots = new Set<string>()
    for (const [p, [b, o]] of scanRows.get(date)!) {
      if (!(P === '' || p.startsWith(P + '/')) || !qp(p)) continue
      let outer = true
      for (let a = parent(p); a.length > P.length; a = parent(a)) if (qp(a)) { outer = false; break }
      if (!outer) continue
      roots.add(p)
      for (let a = p; a.length > P.length; a = parent(a)) { const e = m.get(a) ?? [0, 0]; m.set(a, [e[0] + b, e[1] + o]) }
    }
    return { m, roots }
  }

  /** The nested view's spec, brute force: P's rollup children by name (the drill's kept ones), each drawn over its
   *  level's threshold `T·2^(d−dP−1)`; a drawn non-root child at most two levels below P, at least 64² px and more
   *  than one object, answered (heaviest first, at most `reads`, plus `force`) — a rollup: its kept children, nesting
   *  again; match roots: every path under it with matches, down to the roots. Numbers from the objects only; the
   *  drill says only which answer a path gets and a rollup's kept names. */
  async function bruteNest(t: string, P: string, date: string, T: number, o: { reads?: number; force?: Set<string>; maxDepth?: number; noRoots?: boolean } = {}) {
    const drill = newDrill()
    const { m, roots } = matchedUnder(t, P, date)
    const dP = depthOf(P)
    const thr = (d: number) => T * ATTEN ** Math.max(0, d - dP - 1)
    const capD = dP + Math.min(3, o.maxDepth ?? Infinity)
    const kidsWith = (dir: string) => [...m.keys()].filter(p => parent(p) === dir)
    const named = new Map<string, string[]>()
    const rollupOf = async (dir: string) => {
      const a = await drill.view(t, dir)
      if (!('rollup' in a)) throw new Error(`${t}@${dir}: ${a.source}`)
      named.set(dir, keptOf(a).map(c => dir === '' ? c : `${dir}/${c}`).filter(c => m.has(c)))
    }
    await rollupOf(P)
    const nested = new Set([P])
    const wants = (p: string) => !roots.has(p) && depthOf(p) < capD && (!!o.force?.has(p) || (m.get(p)![0] >= thr(depthOf(p)) && m.get(p)![0] >= T * 4096 / MIN_AREA && m.get(p)![1] > 1))
    let frontier = named.get(P)!.filter(wants)
    let reads = 0, left = 0
    while (frontier.length) {
      frontier.sort((x, y) => m.get(y)![0] - m.get(x)![0] || (x < y ? -1 : 1))
      const next: string[] = []
      for (const p of frontier) {
        if (!o.force?.has(p)) { if (reads >= (o.reads ?? Infinity)) { left++; continue } reads++ }
        const a = await drill.view(t, p)
        if (a.source === 'roots' && o.noRoots) { left++; continue }
        nested.add(p)
        if ('rollup' in a) { await rollupOf(p); next.push(...named.get(p)!.filter(wants)) }
        else if (a.source === 'roots') {
          // The forest: every path under p with matches, children of non-roots.
          const walk = (dir: string) => { const ks = kidsWith(dir); named.set(dir, ks); for (const k of ks) if (!roots.has(k)) walk(k) }
          walk(p)
        } else throw new Error(`${t}@${p}: ${a.source}`)
      }
      frontier = next
    }
    type N = [string, number, number, ...unknown[]]
    const known = new Set([...named.values()].flat())
    const kept = new Set<string>()
    const keep = (dir: string) => { for (const k of named.get(dir) ?? []) if ((o.maxDepth == null || depthOf(k) <= dP + o.maxDepth) && m.get(k)![0] >= thr(depthOf(k))) { kept.add(k); keep(k) } }
    keep(P)
    // The match roots' insides (the light path's phase 2): a drawn root of more than one object and at least 64² px
    // (above `maxDepth`), and every root in `force` this scan holds, reads the unfiltered rows under it, three levels
    // deep (and within `maxDepth`), at its own threshold `T·2^(d−dR−1)`; a row is drawn under a drawn parent. A
    // forced root this scan doesn't hold is answered empty. With no rows left (`noRoots`) none is read.
    const rows = scanRows.get(date)!
    const subdiv = (r: string) => m.get(r)![1] > 1 && m.get(r)![0] >= T * 4096 / MIN_AREA
    const above = (r: string) => o.maxDepth == null || depthOf(r) < dP + o.maxDepth
    const want = [...known].filter(r => roots.has(r) && kept.has(r) && subdiv(r) && above(r))
    const forced = [...o.force ?? []].filter(p => q(t)(p) && !want.includes(p))
    for (const p of forced) if (!roots.has(p)) nested.add(p)
    /** The rows read under the roots: `[bytes, objects]`. */
    const read = new Map<string, [number, number]>()
    for (const R of [...forced.filter(p => roots.has(p) && above(p)), ...want]) {
      if (o.noRoots) { left++; continue }
      nested.add(R)
      const dR = depthOf(R)
      const hi = Math.min(dR + 3, o.maxDepth == null ? Infinity : dP + o.maxDepth)
      const under = [...rows].filter(([p, [b]]) => p.startsWith(`${R}/`) && depthOf(p) <= hi && b >= T * ATTEN ** Math.max(0, depthOf(p) - dR - 1))
      for (const [p, v] of under.sort(([x], [y]) => depthOf(x) - depthOf(y) || (x < y ? -1 : 1))) {
        read.set(p, v)
        named.set(parent(p), [...(named.get(parent(p)) ?? []), p])
        if (kept.has(parent(p))) kept.add(p)
      }
    }
    const val = (p: string) => m.get(p) ?? read.get(p)!
    const build = (dir: string): N[] => {
      const ks = (named.get(dir) ?? []).filter(k => kept.has(k))
      if (!ks.length) return []
      const out: N[] = ks.map(k => { const [b, ob] = val(k); const c = build(k); return [k.split('/').pop()!, b, ob, ...(roots.has(k) ? ['m'] : []), ...(c.length ? [c] : [])] as N })
      const [tb, to] = dir === P ? [...m].filter(([p]) => parent(p) === P).reduce((s, [, [b, ob]]) => [s[0] + b, s[1] + ob], [0, 0]) : val(dir)
      const rest = [tb - out.reduce((s, x) => s + x[1], 0), to - out.reduce((s, x) => s + x[2], 0)]
      if (rest[0] > thr(depthOf(ks[0]))) out.push(['(other)', rest[0], rest[1]])
      return out.sort((x, y) => (x[0] < y[0] ? -1 : 1))
    }
    const matched = [...known].filter(p => roots.has(p)).map(p => [p, ...m.get(p)!]).sort((x, y) => ((x[0] as string) < (y[0] as string) ? -1 : 1))
    // Every path's total, the roots' insides whole (what a lookup inside a root finds).
    const all = new Map(m)
    for (const [p, v] of rows) if ([...roots].some(r => p.startsWith(`${r}/`))) all.set(p, v)
    return { tree: build(P), matched, nested, left, m: all, kept, read, roots }
  }

  /** A view's tree as `bruteNest` spells it: per child `[name, bytes, objects, 'm'?, children?]`, by name. */
  const shape = (n: ViewNode): unknown[] => (n.c ?? []).map(c => [c.n, c.b, c.o, ...(c.m ? ['m'] : []), ...(c.c?.length ? [shape(c)] : [])]).sort((x, y) => ((x[0] as string) < (y[0] as string) ? -1 : 1))

  describe('a heavy literal\'s rollup view nests (the drawn children answered in turn)', () => {
    beforeAll(loadScanRows)

    /** Every (term, path) the drill answers with a rollup (or the catalog, at the fleet root). */
    const rollupViews = async () => {
      const drill = newDrill(), out: [string, string][] = []
      for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) if ('rollup' in await drill.view(t, P)) out.push([t, P])
      return out
    }

    it('node for node = brute force from the objects, at the root and below, both scans, two thresholds', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      let nestedAny = 0
      for (const [t, P] of await rollupViews()) for (const d of DATES) for (const T of [0.5, 40]) {
        const v = await view(envStatic(), d, P, t, { threshold: T })
        const b = await bruteNest(t, P, d, T)
        if (b.nested.size > 1) nestedAny++
        const [tb, to] = [...b.m].filter(([p]) => parent(p) === P).reduce((s, [, [x, y]]) => [s[0] + x, s[1] + y], [0, 0])
        got.push([t, P, d, T, v.tier, v.tree.b, v.tree.o, shape(v.tree), matchedRows(v), v.interiors ?? null])
        want.push([t, P, d, T, 'rollup', tb, to, b.tree, b.matched, null])
      }
      expect(got).toEqual(want)
      expect([got.length, nestedAny]).toEqual([52, 44])
    }, 120_000)  // past 30 s under the full parallel suite (cw, 10-17); the file alone runs 40 tests in ~37 s

    it('a drawn match root\'s inside is the unfiltered view of its path (every descendant matches), `FILTER_SUBDIV_LEVELS` deep', async () => {
      /** Every node under `n` but its `(other)` folds: `[path below n, bytes, objects, kind]`, depth first. */
      const flat = (n: ViewNode, pre = ''): unknown[] => (n.c ?? []).filter(c => c.n !== '(other)')
        .sort((x, y) => (x.n < y.n ? -1 : 1)).flatMap(c => [[`${pre}${c.n}`, c.b, c.o, c.k], ...flat(c, `${pre}${c.n}/`)])
      const got: unknown[] = []
      const want: unknown[] = []
      let inside = 0
      for (const [t, P] of await rollupViews()) for (const d of DATES) for (const T of [0.5, 40]) {
        const v = await view(envStatic(), d, P, t, { threshold: T })
        const roots: [string, ViewNode][] = []
        const walk = (n: ViewNode, p: string) => { for (const c of n.c ?? []) { const cp = p === '' ? c.n : `${p}/${c.n}`; if (c.m) roots.push([cp, c]); else walk(c, cp) } }
        walk(v.tree, P)
        for (const [R, n] of roots) {
          // The plain view's rule for subdividing a root: more than one object, a tile of at least 64² px.
          const subdiv = n.o > 1 && n.b >= T * 4096 / MIN_AREA
          const plain = subdiv ? flat((await buildView(base, { date: d, path: R, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, threshold: T, maxDepth: 3 })).tree) : []
          if (plain.length) inside++
          got.push([t, P, d, T, R, flat(n)])
          want.push([t, P, d, T, R, plain])
        }
      }
      expect(got).toEqual(want)
      expect(inside).toBe(10)
    })

    it('…and the diff\'s rows inside a match root are the unfiltered diff at its path (both sides read it when either subdivides it)', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      let inside = 0
      for (const [t, P] of await rollupViews()) for (const T of [0.5, 40]) {
        const d = await buildDiff(envStatic(), { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t), threshold: T })
        // A root subdivides on a side whose view draws it with more than one object and a tile of at least 64² px.
        const subdiv = new Set<string>()
        for (const date of DATES) {
          const walk = (n: ViewNode, p: string) => { for (const c of n.c ?? []) { const cp = p === '' ? c.n : `${p}/${c.n}`; if (c.m) { if (c.o > 1 && c.b >= T * 4096 / MIN_AREA) subdiv.add(cp) } else walk(c, cp) } }
          walk((await view(envStatic(), date, P, t, { threshold: T })).tree, P)
        }
        const rel = (p: string) => P === '' ? p : p.slice(P.length + 1)
        for (const m of d.matched ?? []) {
          const R = rel(m.path)
          const under = d.rows.filter(r => r.p.startsWith(`${R}/`)).map(r => [r.p.slice(R.length + 1), r.s, r.a, r.b, r.oa, r.ob, !!r.x]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
          const plain = subdiv.has(m.path)
            ? (await buildDiff(base, { from: A, to: B, path: m.path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, threshold: T, depth: 3 })).rows
              .map(r => [r.p, r.s, r.a, r.b, r.oa, r.ob, !!r.x]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
            : []
          if (plain.length) inside++
          got.push([t, P, T, m.path, under])
          want.push([t, P, T, m.path, plain])
        }
      }
      expect(got).toEqual(want)
      expect(inside).toBe(7)
    })

    it('spelled out: `0` at the fleet root draws the bucket, its `fill` and the K = 3 heaviest files there', async () => {
      const v = await view(envStatic(), A, '', '0', { threshold: 0.5 })
      expect(shape(v.tree)).toEqual([
        ['bk', 1023750, 3000, [
          ['fill', 1023750, 3000, [['(other)', 250 * 4095 - 3 * 2048, 2997], ['f00011', 2048, 1, 'm'], ['f00023', 2048, 1, 'm'], ['f00035', 2048, 1, 'm']]],
        ]],
      ])
      expect(v.tree.c![0].c![0].c!.filter(c => c.m).map(c => c.k)).toEqual(['file', 'file', 'file'])
    })

    it('`depth=N` caps the nest at P + N; the first paint and a floor read nest nothing', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const [t, P] of (await rollupViews()).filter(([, P]) => P === '' || P === 'bk')) for (const maxDepth of [1, 2]) {
        const v = await view(envStatic(), A, P, t, { threshold: 0.5, maxDepth })
        got.push([t, P, maxDepth, shape(v.tree)])
        want.push([t, P, maxDepth, (await bruteNest(t, P, A, 0.5, { maxDepth })).tree])
      }
      expect(got).toEqual(want)
      const fp = await view(envStatic(), A, '', '0', { threshold: 0.5, firstPaint: true })
      expect(shape(fp.tree)).toEqual([['bk', 1023750, 3000]])
    })

    it('the read budget: past `nestReads` answers a child stays one exact tile, and `interiors` says how many', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const [t, P] of await rollupViews()) for (const reads of [0, 1]) {
        const v = await view(envStatic(), A, P, t, { threshold: 0.5, nestReads: reads })
        const b = await bruteNest(t, P, A, 0.5, { reads })
        got.push([t, P, reads, shape(v.tree), v.interiors ?? null])
        want.push([t, P, reads, b.tree, b.left ? { read: b.nested.size - 1, skipped: b.left, reason: `${b.left} over the read budget` } : null])
      }
      expect(got).toEqual(want)
      expect(got.filter(g => (g as unknown[])[4]).length).toBe(17)
    })

    it('the rows budget: a roots answer bounded past the rows left is not read (one exact tile); rollups still nest', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const [t, P] of await rollupViews()) {
        const v = await view(envStatic(), A, P, t, { threshold: 0.5, phase2Rows: 0 })
        const b = await bruteNest(t, P, A, 0.5, { noRoots: true })
        got.push([t, P, shape(v.tree), v.interiors ?? null])
        want.push([t, P, b.tree, b.left ? { read: b.nested.size - 1, skipped: b.left, reason: `${b.left} over the read budget` } : null])
      }
      expect(got).toEqual(want)
      expect(got.filter(g => (g as unknown[])[3]).length).toBe(7)
    })

    it('the time budget: a nest past it is `late` (kept briefly, upgraded), its totals still exact', async () => {
      const slow: HitSource = { hits: async (k, u) => u === '' || u === 'bk' ? drillSource(drillBlobs()).hits(k, u) : new Promise(r => setTimeout(() => r(drillSource(drillBlobs()).hits(k, u)), 200)) }
      const v = await view(envStatic(heavyStore(slow)), A, '', '0', { threshold: 0.5, phase2Ms: 50 })
      expect([shape(v.tree), v.interiors, v.tree.b, v.tree.o]).toEqual([
        [['bk', 1023750, 3000, [['fill', 1023750, 3000]]]],
        { read: 1, skipped: 1, reason: '1 past the time budget', late: 1 },
        1023750, 3000,
      ])
    })

    it('the diff nests both sides alike (each nests every path either answered): every row = brute force from the objects', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      let deep = 0, forced = 0
      for (const [t, P] of await rollupViews()) for (const T of [0.5, 40, 300, 2000]) {
        const d = await buildDiff(envStatic(), { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t), threshold: T })
        const [a0, b0] = [await bruteNest(t, P, A, T), await bruteNest(t, P, B, T)]
        const force = new Set([...a0.nested, ...b0.nested])
        if (force.size > Math.min(a0.nested.size, b0.nested.size)) forced++
        const [sa, sb] = [await bruteNest(t, P, A, T, { force }), await bruteNest(t, P, B, T, { force })]
        const top = (s: typeof sa): [number, number] => [...s.m].filter(([p]) => parent(p) === P).reduce<[number, number]>((x, [, [b, o]]) => [x[0] + b, x[1] + o], [0, 0])
        const at = (s: typeof sa, p: string): [number, number] | null => (p === P ? top(s) : s.m.get(p)) ?? null
        const st = (x: [number, number] | null, y: [number, number] | null): DiffRow['s'] => !x ? 'added' : !y ? 'removed' : x[0] !== y[0] || x[1] !== y[1] ? 'changed' : 'unchanged'
        const rows: unknown[][] = []
        const rel = (p: string) => P === '' ? p : p.slice(P.length + 1)
        // At or inside a match root a side's undrawn name is no known zero: it reads the side's read rows, else a
        // lookup (one per name the side's own roots hold) — flagged `l` (the first side that took one).
        const inMatch = (p: string) => { for (let a = p; a.length > P.length; a = parent(a)) { if (q(t)(a)) return true; if (a === '') break } return false }
        const inRoots = (s: typeof sa, c: string) => [...s.roots].some(r => c.startsWith(`${r}/`))
        let lookups = 0
        const walk = (p: string, l?: 1 | 2) => {
          const x = at(sa, p), y = at(sb, p)
          const names = [...new Set([...sa.kept, ...sb.kept])].filter(c => parent(c) === p).sort()
          const expand = !!(x || y) && st(x, y) !== 'unchanged' && names.length > 0
          if (p !== P) rows.push([rel(p), st(x, y), x?.[0] ?? 0, y?.[0] ?? 0, x?.[1] ?? 0, y?.[1] ?? 0, expand, ...(l ? [l] : [])])
          if (!expand) return
          for (const s of [sa, sb]) lookups += names.filter(c => !s.kept.has(c) && !s.read.has(c) && inRoots(s, c)).length
          for (const c of names) walk(c, at(sa, c) && !sa.kept.has(c) && inMatch(p) ? 1 : at(sb, c) && !sb.kept.has(c) && inMatch(p) ? 2 : undefined)
          const sum = (s: typeof sa) => names.reduce<[number, number]>((r, c) => { const v = at(s, c); return v ? [r[0] + v[0], r[1] + v[1]] : r }, [0, 0])
          const ra: [number, number] = [(x?.[0] ?? 0) - sum(sa)[0], (x?.[1] ?? 0) - sum(sa)[1]], rb: [number, number] = [(y?.[0] ?? 0) - sum(sb)[0], (y?.[1] ?? 0) - sum(sb)[1]]
          if (ra[0] > 0 || rb[0] > 0) rows.push([p === P ? '(other)' : `${rel(p)}/(other)`, st(ra[0] > 0 ? ra : null, rb[0] > 0 ? rb : null), Math.max(0, ra[0]), Math.max(0, rb[0]), ra[0] > 0 ? ra[1] : 0, rb[0] > 0 ? rb[1] : 0, false])
        }
        walk(P)
        if (rows.some(r => (r[0] as string).includes('/'))) deep++
        const byPath = (rs: unknown[][]) => rs.sort((x, y) => ((x[0] as string) < (y[0] as string) ? -1 : 1))
        got.push([t, P, T, d.total_a, d.total_b, d.objects_a, d.objects_b, d.lookups, byPath(d.rows.map(r => [r.p, r.s, r.a, r.b, r.oa, r.ob, !!r.x, ...(r.l ? [r.l] : [])]))])
        want.push([t, P, T, top(sa)[0], top(sb)[0], top(sa)[1], top(sb)[1], lookups, byPath(rows.filter(r => r[6] || r[1] !== 'unchanged'))])
      }
      expect(got).toEqual(want)
      // Inside the roots: some names a side didn't draw come from its read rows, some from lookups.
      const looked = got.filter(g => (g as unknown[])[7] as number > 0).length
      const flagged = got.flatMap(g => (g as unknown[][])[8]).filter(r => (r as unknown[]).length === 8).length
      expect([got.length, deep, forced, looked, flagged]).toEqual([52, 24, 16, 11, 0])
    }, 120_000)  // 52 brute-force diffs: ~5 s alone, 43 s under the full suite's parallel load (gcs, cw: past the 30 s default)

    it('inside a roots answer a folder drawn on one side only takes the other side\'s total from the answer, not a lookup', async () => {
      // `bin` at the fleet root: a rollup of `bk`, whose answer is its roots — `bk/s/y/a.bin` (100 → 30 bytes) and
      // `bk/s/z/b.bin` (5000 → 6000). At T = 10, `bk/s/y` (depth 3: 40 bytes) is drawn on A only, nonzero on B.
      const [vA, vB, open] = [scanAt(A), scanAt(B), 4291747200000]
      const hit = (path: string, vf: number, vt: number, size: number): Hit => ({ path, depth: path.split('/').length, usr: '', vf, vt, size: BigInt(size), n: 1n })
      const cell = (vf: number, b: number) => ({ kind: 1 as const, child: 'bk', vf, b: BigInt(b), o: 2n })
      const src: HitSource = {
        async hits(_k, u) {
          if (u === '') return { rollup: { dir: '', kept: 1, rows: 3, children: 1, cells: [cell(vA, 5100), cell(vB, 6030)] }, io: { from: 'drill' } }
          if (u === 'bk') return { hits: [hit('bk/s/y/a.bin', vA, vB, 100), hit('bk/s/y/a.bin', vB, open, 30), hit('bk/s/z/b.bin', vA, vB, 5000), hit('bk/s/z/b.bin', vB, open, 6000)], io: { from: 'drill' } }
          return null
        },
      }
      const d = await buildDiff(envStatic(heavyStore(src)), { from: A, to: B, path: '', w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 100, query: q('bin'), threshold: 10 })
      expect([d.lookups, d.rows.map(r => [r.p, r.s, r.a, r.b, r.x ?? null, r.l ?? null])]).toEqual([0, [
        ['bk', 'changed', 5100, 6030, true, null],
        ['bk/s', 'changed', 5100, 6030, true, null],
        ['bk/s/y', 'changed', 100, 30, true, null],
        ['bk/s/z', 'changed', 5000, 6000, true, null],
        ['bk/s/z/b.bin', 'changed', 5000, 6000, null, null],
        ['bk/s/y/a.bin', 'changed', 100, 30, null, null],
      ]])
    })

    it('a light literal (its roots answer at P) is unchanged: the same view as the search sidecars\'', async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of ['tomat', 'bin', 'x', 'om']) for (const P of ['', 'bk', 'bk/data']) {
        if ((await newDrill().view(t, P)).source !== 'roots') continue
        const [s, r] = await Promise.all([view(envStatic(), A, P, t), view(base, A, P, t)])
        got.push([t, P, sansP1(s), matchedRows(s)])
        want.push([t, P, sansP1(r), matchedRows(r)])
      }
      expect(got).toEqual(want)
      expect(got.length).toBe(10)
    })

    it('a filtered diff\'s rows inside a match root are each path\'s true totals on both scans, its lookups cut or not', async () => {
      // A name one side drew inside a root (over its interior threshold there, under it or unread on the other side)
      // takes the other side's total from a lookup; one the lookup budget leaves unanswered (`lookupCap`, the walk's
      // time) is folded into its parent's `(other)` on both sides, never drawn as added / removed at zero (cw 10-15:
      // `q=feuer`, the walk past `FILTER_WALK_MS`, a 2.5 GB dir read as "added").
      const bad: unknown[] = []
      let rows = 0, folded = 0, lateCases = 0, looked = 0
      for (const [env, src] of [[envStatic(), 'static'], [base, 'search']] as const) for (const t of [...TERMS, 'bk']) for (const P of ['', 'bk', 'bk/data']) for (const T of [0.5, 40, 300, 2000, 2300]) {
        const o = { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t), threshold: T }
        // A query the static source refuses here (`FilterRejected`), or a path a scan lacks: no diff to check.
        const full = await buildDiff(env, o).catch(e => { if (e instanceof FilterRejected || e instanceof NotFound) return null; throw e })
        if (!full) continue
        for (const cut of [{}, { lookupCap: 0 }, { walkMs: 0 }]) {
          const d: Diff = Object.keys(cut).length ? await buildDiff(env, { ...o, ...cut }) : full
          if (d.lookups_late) lateCases++
          const roots = (d.matched ?? []).map(m => m.path)
          const drawn = d.rows.filter(r => !r.p.endsWith('(other)')).map(r => [P === '' ? r.p : `${P}/${r.p}`, r] as const).filter(([p]) => roots.some(R => p.startsWith(`${R}/`)))
          rows += drawn.length; looked += drawn.filter(([, r]) => r.l).length
          if (d !== full) folded += full.rows.filter(r => !r.p.endsWith('(other)')).length - d.rows.filter(r => !r.p.endsWith('(other)')).length
          for (const [p, r] of drawn) {
            const [a, oa] = scanRows.get(A)!.get(p) ?? [0, 0], [b, ob] = scanRows.get(B)!.get(p) ?? [0, 0]
            const s = !a && !oa ? 'added' : !b && !ob ? 'removed' : a !== b || oa !== ob ? 'changed' : 'unchanged'
            if (r.a !== a || r.b !== b || r.oa !== oa || r.ob !== ob || r.s !== s) bad.push([src, t, P, T, cut, r.p, [r.s, r.a, r.b, r.oa, r.ob], [s, a, b, oa, ob]])
          }
        }
      }
      expect(bad).toEqual([])
      expect([rows, looked, folded, lateCases]).toEqual([229, 1, 416, 100])
    })
  })

  // --- the fleet root of a heavy literal, nested to the plain view's depth (cw 10-13: `?f=.json` drew bucket → top
  // dirs only, its phase 2 cut by the viewer's time budget on a cold isolate, and the cut answer stayed) ------------

  describe('a heavy literal at the fleet root nests root → bucket → dir → subdir; a budget-cut answer is upgraded', () => {
    // The objects (all live from A): `bin` matches the files only. The drill answers `''`, `b1` and `b1/marin` with
    // rollups (every child kept by name), anything else with its match roots (`b1/iris`: a forest).
    const OBJECTS: [string, number][] = [
      ['b1/marin/raw/a/x.bin', 4000], ['b1/marin/raw/b/y.bin', 3000], ['b1/marin/evals/e1.bin', 1500], ['b1/marin/evals/e2.bin', 1000],
      ['b1/iris/cp1/s1.bin', 2000], ['b1/iris/cp1/s2.bin', 2000], ['b1/iris/cp2/s.bin', 2500],
      ['b1/tmp/t.bin', 300],
      ['b2/z.bin', 200],
    ]
    const ROLLUPS = new Set(['', 'b1', 'b1/marin'])
    const open = 4291747200000
    const under = (p: string, P: string) => P === '' || p.startsWith(`${P}/`)
    /** Brute force: `[bytes, objects]` of the objects at or under `P`. */
    const sum = (P: string): [number, number] => OBJECTS.filter(([p]) => p === P || under(p, P)).reduce<[number, number]>((s, [, b]) => [s[0] + b, s[1] + 1], [0, 0])
    const source = (delayMs = 0, asked: string[] = []): HitSource => ({
      async hits(_k, u) {
        asked.push(u)
        if (delayMs && !['', 'b1'].includes(u)) await new Promise(r => setTimeout(r, delayMs))
        const objs = OBJECTS.filter(([p]) => under(p, u))
        if (!objs.length) return null
        if (ROLLUPS.has(u)) {
          const kids = [...new Set(objs.map(([p]) => (u === '' ? p : p.slice(u.length + 1)).split('/')[0]))].sort()
          const cells = kids.map(c => { const [b, o] = sum(u === '' ? c : `${u}/${c}`); return { kind: 1 as const, child: c, vf: scanAt(A), b: BigInt(b), o: BigInt(o) } })
          return { rollup: { dir: u, kept: kids.length, rows: objs.length, children: kids.length, cells }, io: { from: 'drill' } }
        }
        return { hits: objs.map(([path, size]) => ({ path, depth: depthOf(path), usr: '', vf: scanAt(A), vt: open, size: BigInt(size), n: 1n })), io: { from: 'drill' } }
      },
    })
    /** `[name, bytes, objects, 'm'?, children?]` from brute force: the node at `p`, its listed children. */
    const node = (p: string, kids: unknown[] = []): unknown[] => { const [b, o] = sum(p); return [p.split('/').pop()!, b, o, ...(p.endsWith('.bin') ? ['m'] : []), ...(kids.length ? [kids] : [])] }
    /** The plain filter view's depth below the root (`FILTER_SUBDIV_LEVELS` = 3): each answered path's children,
     *  a roots answer's whole forest. `b1/marin/{raw,evals}` (depth 3) are not answered; `b1/tmp`, `b2` hold one object. */
    const NESTED = [
      node('b1', [
        node('b1/iris', [node('b1/iris/cp1', [node('b1/iris/cp1/s1.bin'), node('b1/iris/cp1/s2.bin')]), node('b1/iris/cp2', [node('b1/iris/cp2/s.bin')])]),
        node('b1/marin', [node('b1/marin/evals'), node('b1/marin/raw')]),
        node('b1/tmp'),
      ]),
      node('b2'),
    ]
    /** What the viewer's budget leaves when every answer below the bucket is late: bucket → top dirs. */
    const CUT = [node('b1', [node('b1/iris'), node('b1/marin'), node('b1/tmp')]), node('b2')]

    it('the root view nests three levels, node for node = brute force; its match roots exact', async () => {
      const asked: string[] = []
      const v = await view(envStatic(heavyStore(source(0, asked))), A, '', 'bin', { threshold: 0.5 })
      expect([v.tier, v.tree.b, v.tree.o, shape(v.tree), matchedRows(v), v.interiors ?? null, asked.sort()]).toEqual([
        'rollup', ...sum(''), NESTED,
        OBJECTS.filter(([p]) => p.startsWith('b1/iris/')).map(([p, b]) => [p, b, 1]).sort((x, y) => (x[0] < y[0] ? -1 : 1)),
        null,
        // `b1/tmp` and `b2` (one object each) and depth 3 (`b1/marin/*`) are never asked.
        ['', 'b1', 'b1/iris', 'b1/marin'],
      ])
    })

    it('`/api/subtree`: the cold answer is cut (`late`, browser copy `no-store`); the background run replaces it with the nested one', async () => {
      const held = new Map<string, Response>()
      const prev = (globalThis as unknown as { caches: unknown }).caches
      ;(globalThis as unknown as { caches: unknown }).caches = { default: {
        match: async (k: Request) => held.get(k.url)?.clone(),
        put: async (k: Request, r: Response) => { held.set(k.url, r) },
      } }
      resetUpgrades()
      const env = envStatic(heavyStore(source(100)))
      Object.assign(env, { FILTER_PHASE2_MS: '5', DEV_EMAIL: 'dev@example.test', STORE_BUCKET: 'my-data' })
      const bg: Promise<unknown>[] = []
      const waitUntil = (p: Promise<unknown>) => { bg.push(p) }
      const url = `http://localhost/api/subtree?date=${A}&path=&q=bin&full=1&w=${W}&h=${H}&minArea=${MIN_AREA}&atten=${ATTEN}`
      const said = async (r: Response) => {
        const j = await r.json() as { budgetCut?: true; interiors?: unknown; tree: ViewNode }
        return [r.headers.get('x-cache'), r.headers.get('x-cache-partial'), r.headers.get('cache-control'), r.headers.get('x-cache-upgrade'), j.budgetCut ?? null, j.interiors ?? null, shape(j.tree)]
      }
      try {
        const cold = await said(await warmSubtree(env, url, waitUntil))
        const reload = await said(await warmSubtree(env, url, waitUntil))
        await Promise.all(bg)
        const after = await said(await warmSubtree(env, url, waitUntil))
        expect([cold, reload, after]).toEqual([
          ['miss', 'ttl=120', 'no-store', 'scheduled', true, { read: 1, skipped: 2, reason: '2 past the time budget', late: 2 }, CUT],
          ['hit', 'ttl=120', 'no-store', 'in-flight', true, { read: 1, skipped: 2, reason: '2 past the time budget', late: 2 }, CUT],
          ['hit', null, 'private, max-age=300', null, null, null, NESTED],
        ])
      } finally {
        ;(globalThis as unknown as { caches: unknown }).caches = prev
      }
    })
  })
})
