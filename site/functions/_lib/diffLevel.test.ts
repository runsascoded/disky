import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { buildLevels, type Level, levelK, levelOf, type LevelRow, mergeLevels } from './diffLevel'
import { asOfScans, chargeLevel, coloPutsSettled, GetRounds, LEVEL_BUDGET, levelHeldBytes, openIndex, planChanged, planRects, type Rect, withChurn } from './index'
import { ahead, clearIsolate, loadScopes } from './shared'
import { sqliteD1 } from './testD1'
import { fixtureR2 as r2 } from './testR2'
import { readJson } from './testStore'
import { buildDiff, buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// `fixtures/iv-churn/`: a churn-carrying interval store (a base over four scans, three appended as runs, merged by the
// carries), 4 rows per group (`gen.py`). `expected.json`: per `(from, to, path)` from the churn base on, the level
// answer computed from the scans' files alone.

type Want = { from: string; to: string; path: string; a: number; b: number; oa: number; ob: number; churn: number; rows: { p: string; k: 'file' | 'dir'; a: number; b: number; oa: number; ob: number; churn: number }[] }

/** The oracle's answer (the fixture's `a`/`b`/`oa`/`ob`) in the level's shape: rows by name under the path, each with
 *  its status. */
function wanted(w: Want): Pick<Level, 'path' | 'bytes_before' | 'bytes_after' | 'objects_before' | 'objects_after' | 'churn' | 'rows' | 'unchanged'> {
  const rows = w.rows.map(({ p, k, a, b, oa, ob, churn }): LevelRow => ({
    name: w.path ? p.slice(w.path.length + 1) : p, kind: k, bytes_before: a, bytes_after: b, objects_before: oa, objects_after: ob, churn,
    status: !oa && !ob ? 'changed' : !oa ? 'added' : !ob ? 'removed' : 'changed',
  }))
  const sum = (f: 'bytes_before' | 'bytes_after' | 'objects_before' | 'objects_after') => rows.reduce((n, r) => n + r[f], 0)
  return {
    path: w.path, bytes_before: w.a, bytes_after: w.b, objects_before: w.oa, objects_after: w.ob, churn: w.churn, rows: byName(rows),
    unchanged: { bytes_before: w.a - sum('bytes_before'), bytes_after: w.b - sum('bytes_after'), objects_before: w.oa - sum('objects_before'), objects_after: w.ob - sum('objects_after') },
  }
}
const byName = (rows: LevelRow[]): LevelRow[] => [...rows].sort((x, y) => (x.name < y.name ? -1 : 1))
const shape = (l: Level) => ({
  path: l.path, bytes_before: l.bytes_before, bytes_after: l.bytes_after, objects_before: l.objects_before, objects_after: l.objects_after, churn: l.churn,
  rows: byName(l.rows), unchanged: l.unchanged,
})

let env: Env
let reads: string[]
let cases: Want[]

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1()
  reads = []
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INDEX_R2: r2('iv-churn', reads) } as Env
  cases = await readJson<Want[]>('iv-churn/expected.json')
})

describe('level diffs', () => {
  it('answers every directory as the scans\' files do: changed children, range churn, unchanged by subtraction', async () => {
    expect(cases.length).toBe(60)
    const got = []
    for (const c of cases) {
      const l = (await buildLevels(env, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false })).get(c.path)!
      expect(l.other).toBeNull()
      got.push(shape(l))
    }
    expect(got).toEqual(cases.map(wanted))
    // Renames and swaps: some child's churn exceeds its net change.
    expect(cases.some(c => c.rows.some(r => r.churn > Math.abs(r.b - r.a)))).toBe(true)
  })

  it('answers a batch of directories as it answers each alone', async () => {
    const pairs = [...new Set(cases.map(c => `${c.from}|${c.to}`))]
    for (const pr of pairs) {
      const [from, to] = pr.split('|')
      const dirs = cases.filter(c => c.from === from && c.to === to).map(c => c.path)
      const batch = await buildLevels(env, { from, to, dirs, k: 512, m: 'churn', chain: false })
      const alone = await Promise.all(dirs.map(async d => (await buildLevels(env, { from, to, dirs: [d], k: 512, m: 'churn', chain: false })).get(d)!))
      expect(dirs.map(d => batch.get(d))).toEqual(alone)
    }
  })

  it('follows a directory whose only changed child is a directory, in the same request', async () => {
    const c = cases.find(x => x.path === '' && x.rows.length === 1 && x.rows[0].k === 'dir')!
    const child = c.rows[0].p
    const got = await buildLevels(env, { from: c.from, to: c.to, dirs: [''], k: 512, m: 'churn' })
    const root = got.get('')!
    expect(root.next).toBe(child)
    const want = cases.find(x => x.from === c.from && x.to === c.to && x.path === child)!
    expect(shape(got.get(child)!)).toEqual(wanted(want))
  })

  it('folds the children past K into `other`, ranked by the measure', () => {
    const own = { bytes_before: 100, bytes_after: 100, objects_before: 10, objects_after: 10, churn: 60 }
    const kid = (p: string, a: number, b: number, churn: number) =>
      ({ path: `d/${p}`, kind: 'file' as const, status: 'changed' as const, bytes_before: a, bytes_after: b, objects_before: 1, objects_after: 1, churn })
    const kids = [kid('x', 10, 10, 30), kid('y', 10, 30, 20), kid('z', 5, 0, 5), kid('w', 7, 7, 0)]
    const byChurn = levelOf('d', own, kids, 2, 'churn')
    expect([byChurn.rows.map(r => r.name), byChurn.other, byChurn.unchanged]).toEqual([
      ['x', 'y'],
      { count: 1, bytes_before: 5, bytes_after: 0, objects_before: 1, objects_after: 1, churn: 5 },
      { bytes_before: 75, bytes_after: 60, objects_before: 7, objects_after: 7 },
    ])
    expect(levelOf('d', own, kids, 2, 'net').rows.map(r => r.name)).toEqual(['y', 'z'])
    expect([levelK(1), levelK(5), levelK(100), levelK(1e6)]).toEqual([4, 8, 128, 512])
  })

  it('merges stores\' answers for one directory as one store holding all of them answers it (a union view\'s root)', async () => {
    // `interval-store-b1/`, `-b2/`: the same scans' files under `b1`, `b2` only (`gen.py b1|b2`): disjoint roots, as a
    // union's members are. Each store's root answer, merged, is the whole store's, at any K (cut after the merge).
    const store = async (prefix: string) => ({ DB: (await sqliteD1()).db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: prefix, INDEX_R2: r2('iv-churn', []) }) as Env
    const [b1, b2] = await Promise.all([store('interval-store-b1'), store('interval-store-b2')])
    const roots = cases.filter(c => c.path === '')
    expect(roots.length).toBe(10)
    const got: Level[] = [], want: Level[] = []
    for (const c of roots) {
      for (const k of [1, 512]) {
        const o = { from: c.from, to: c.to, dirs: [''], k, m: 'churn' as const, chain: false }
        const parts = await Promise.all([b1, b2].map(async e => (await buildLevels(e, o)).get('')!))
        got.push(mergeLevels('', parts, k, 'churn'))
        want.push((await buildLevels(env, o)).get('')!)
      }
    }
    expect(got).toEqual(want)
    // Both roots changed in some range (a real merge), and K = 1 cut one of them into `other`.
    expect(want.some(l => l.other?.count === 1)).toBe(true)
    // Below the root, a directory's answer is its own store's.
    for (const c of cases.filter(x => x.path.startsWith('b1/'))) {
      const o = { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn' as const, chain: false }
      expect((await buildLevels(b1, o)).get(c.path)).toEqual((await buildLevels(env, o)).get(c.path))
    }
  })

  it('merges by the measure, ranking and cutting as `levelOf` does', () => {
    const kid = (path: string, a: number, b: number, churn: number) =>
      ({ path, kind: 'dir' as const, status: 'changed' as const, bytes_before: a, bytes_after: b, objects_before: 1, objects_after: 1, churn })
    const kids = [kid('x', 10, 10, 30), kid('y', 10, 30, 20), kid('z', 5, 0, 5), kid('w', 7, 7, 0), kid('v', 1, 2, 1)]
    const sum = (ks: typeof kids, extra: number) => ({
      bytes_before: ks.reduce((n, c) => n + c.bytes_before, extra), bytes_after: ks.reduce((n, c) => n + c.bytes_after, extra),
      objects_before: ks.length + extra, objects_after: ks.length + extra, churn: ks.reduce((n, c) => n + c.churn, 0),
    })
    const [m1, m2] = [[kids[0], kids[2], kids[4]], [kids[1], kids[3]]]
    for (const m of ['churn', 'net'] as const) {
      for (const k of [1, 2, 3, 8]) {
        const parts = [levelOf('', sum(m1, 4), m1, k, m), levelOf('', sum(m2, 9), m2, k, m)]
        expect(mergeLevels('', parts, k, m)).toEqual(levelOf('', sum(kids, 13), kids, k, m))
      }
    }
    // One member's only change, a directory, and none in the other: the merged root leads into it.
    const one = mergeLevels('', [levelOf('', sum([kids[0]], 0), [kids[0]], 4, 'churn'), levelOf('', sum([], 3), [], 4, 'churn')], 4, 'churn')
    expect(one.next).toBe('x')
  })

  it('refuses a range from before the churn base', async () => {
    await expect(buildLevels(env, { from: '2026-07-31', to: '2026-08-03', dirs: [''], k: 8, m: 'churn' })).rejects.toThrow(/churn is counted from/)
  })

  it('cut in pages with a page index (2b.1), answers exactly as whole groups, decoding fewer rows than they hold', async () => {
    // `interval-store-p2/`: the same store cut in 2-row pages (`gen.py 2`); its own prefix keeps its groups apart in
    // the isolate's cache, so every read here is by page or whole by its own index.
    const { db } = await sqliteD1()
    const paged = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: 'interval-store-p2', INDEX_R2: r2('iv-churn', []) } as Env
    const got = []
    const counts = { pgroups: 0, pages: 0, pskip: 0, prows: 0 }
    const decoded: [number, number][] = []
    for (const c of cases) {
      const t: Record<string, number> = {}
      const trace = (name: string, v: number) => { t[name] = (t[name] ?? 0) + v }
      got.push(shape((await buildLevels(paged, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false, trace })).get(c.path)!))
      for (const k of Object.keys(counts) as (keyof typeof counts)[]) counts[k] += t[k] ?? 0
      decoded.push([t.prows ?? 0, t.pgroups ?? 0])
    }
    expect(got).toEqual(cases.map(wanted))
    // Pages were read and skipped, and a group read by page decodes fewer rows than its 4 (it'd be read whole otherwise).
    expect([counts.pgroups > 0, counts.pskip > 0, counts.pages > 0]).toEqual([true, true, true])
    expect(decoded.filter(([rows, groups]) => rows >= 4 * groups && groups > 0)).toEqual([])
  })

  describe('a generation whose tiers were cut across a change of page settings (base and runs, some with a page index, some without)', () => {
    // `interval-store/` (no page index: what the cut writes) and `-p2/` (2-row pages and a page index: what it wrote from
    // 2b.1 until pages went back to the level sort alone) are the same store, so each tier — the base's `served/`, or a
    // run — can come from either. A run cut in that time keeps its pages until a carry or a compaction rewrites it.
    const PLAIN = 'interval-store'
    const PAGED = 'interval-store-p2'
    const MERGED = 'deltas/2026-08-04_2026-08-05'
    const NEWEST = 'deltas/2026-08-06'
    const tierOf = (rest: string): string | null => /^g\/(deltas\/[^/]+)\//.exec(rest)?.[1] ?? (rest.startsWith('g/served/') ? 'base' : null)
    const mixed = (name: string, from: (tier: string) => string, reads: string[]): R2Bucket => {
      const under = r2('iv-churn', reads)
      const real = (key: string): string => {
        const rest = key.slice(name.length + 1)
        const tier = tierOf(rest)
        return `${tier ? from(tier) : PLAIN}/${rest}`
      }
      return {
        get: (key: string, o?: R2GetOptions) => under.get(real(key), o),
        head: (key: string) => under.head(real(key)),
        list: async ({ prefix }: { prefix: string }) => {
          const { objects, truncated } = await under.list({ prefix: real(prefix) })
          return { truncated, objects: objects.map(x => ({ ...x, key: x.key.replace(/^[^/]+\//, `${name}/`) })) }
        },
      } as unknown as R2Bucket
    }
    const envOf = async (prefix: string, bucket: R2Bucket) => ({ DB: (await sqliteD1()).db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: prefix, INDEX_R2: bucket }) as Env
    /** The `path` data files read, as `<store> <tier>`. */
    const tiersRead = (reads: string[]): string[] => [...new Set(reads.filter(k => k.endsWith('/served/path.parquet')).map(k => `${k.split('/')[0]} ${tierOf(k.slice(k.indexOf('/') + 1))}`))].sort()
    const SCANS = ['2026-07-30', '2026-07-31', '2026-08-02', '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06']
    const DIRS = ['', 'b1', 'b1/x', 'b2', 'b2/z']

    it.each([
      ['a paged base under runs without an index', 'interval-store-mix-a', (t: string) => (t === 'base' ? PAGED : PLAIN),
        [`${PAGED} base`, `${PLAIN} ${MERGED}`, `${PLAIN} ${NEWEST}`]],
      ['a base without an index under paged runs', 'interval-store-mix-b', (t: string) => (t === 'base' ? PLAIN : PAGED),
        [`${PLAIN} base`, `${PAGED} ${MERGED}`, `${PAGED} ${NEWEST}`]],
      ['a base without an index, a merged run with one, a newer run without (a store once the cut stops paging `path`)', 'interval-store-mix-c', (t: string) => (t === MERGED ? PAGED : PLAIN),
        [`${PLAIN} base`, `${PAGED} ${MERGED}`, `${PLAIN} ${NEWEST}`]],
    ])('%s: levels, Map views and walk diffs are the unpaged store\'s, each tier read as it was cut', async (_, name, from, tiers) => {
      const reads: string[] = []
      const env = await envOf(name, mixed(name, from, reads))
      const plain = await envOf(PLAIN, r2('iv-churn', []))

      // `/api/diff/level`: the one reader of `path`'s pages. A group with an index is read by page, one without whole.
      const got = []
      const counts = { pgroups: 0, pages: 0 }
      for (const c of cases) {
        const trace = (k: string, v: number) => { if (k in counts) counts[k as keyof typeof counts] += v }
        got.push(shape((await buildLevels(env, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false, trace })).get(c.path)!))
      }
      expect(got).toEqual(cases.map(wanted))
      expect([counts.pgroups > 0, counts.pages > 0]).toEqual([true, true])
      expect(tiersRead(reads)).toEqual([...tiers].sort())

      // The Map and the walk diff: whole groups from every tier, no page index asked for, the same answers.
      const paged: string[] = []
      const trace = (k: string) => { if (/^p(groups|ages|skip|rows|index|data|json|gets|rounds)/.test(k)) paged.push(k) }
      reads.length = 0
      for (const date of SCANS) {
        for (const path of DIRS) {
          const o = { date, path, w: 8, h: 6, minArea: 12, atten: 2 }
          expect(await buildView(env, { ...o, trace })).toEqual(await buildView(plain, o))
        }
      }
      for (let i = 1; i < SCANS.length; i++) {
        for (const path of DIRS) {
          const o = { from: SCANS[i - 1], to: SCANS[i], path, w: 8, h: 6, minArea: 12, atten: 2, top: 100 }
          expect(await buildDiff(env, { ...o, trace })).toEqual(await buildDiff(plain, o))
        }
      }
      expect(paged).toEqual([])
      expect(tiersRead(reads)).toEqual([...tiers].sort())
    })
  })

  it('counts GET rounds: a slot-bound GET is in the round after the one freeing its slot; colo lookups add none', async () => {
    const lim = new GetRounds(2)
    const get = (dep: number, free = false) => lim.run(dep, async () => null, free).then(r => r.round)
    // Three independent GETs on two slots: the third waits a round; one depending on round 1 is in round 2; a colo
    // lookup holds a slot but adds no round: queued, it is in the round of the GET whose slot it took.
    expect(await Promise.all([get(0), get(0), get(0), get(1), get(0, true)])).toEqual([1, 1, 2, 2, 2])
    expect([lim.gets, lim.rounds]).toEqual([4, 2])
    const free = new GetRounds(2)
    expect(await free.run(0, async () => null, true).then(r => r.round)).toBe(0)
    expect(await free.run(0, async () => null).then(r => r.round)).toBe(1)
    expect([free.gets, free.rounds]).toEqual([1, 1])
  })

  it('a paged level request: the page index is a wave of GETs only cold; held (isolate or colo), the pages are the only GETs', async () => {
    // Each step on a fresh module graph is a fresh isolate (its footers, groups and page indexes unheld); the colo is
    // a cache kept across them.
    const c = cases.find(x => x.from === '2026-08-02' && x.to === '2026-08-06' && x.path === '')!
    const held = new Map<string, ArrayBuffer>()
    const colo = {
      match: async (r: Request) => (held.has(r.url) ? new Response(held.get(r.url)!.slice(0)) : undefined),
      put: async (r: Request, res: Response) => { held.set(r.url, await res.arrayBuffer()) },
    }
    const g = globalThis as unknown as { caches: unknown }
    const prev = g.caches
    g.caches = { default: colo }
    const log: string[] = []
    const { db } = await sqliteD1()
    const paged = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: 'interval-store-p2', INDEX_R2: r2('iv-churn', log) } as Env
    const KEYS = ['pindex-gets', 'pindex-held', 'pindex-colo', 'pdata-gets', 'pgroups', 'pgets', 'prounds'] as const
    const request = async (build: typeof buildLevels) => {
      const t: Record<string, number> = {}
      const at = log.length
      const l = (await build(paged, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false, trace: (name, v) => { t[name] = (t[name] ?? 0) + v } })).get(c.path)!
      expect(shape(l)).toEqual(wanted(c))
      // The data files' GETs (the base's and each run's sort), from the store's log: the paged read's (`pgets`), then
      // the whole groups' (`readChanged` reads those after it; here, 4-row groups whose every 2-row page is kept).
      return { ...Object.fromEntries(KEYS.map(k => [k, t[k]])), files: log.slice(at).filter(k => k.endsWith('/served/path.parquet')).length }
    }
    const fresh = async () => {
      vi.resetModules()
      return (await import('./diffLevel')).buildLevels
    }
    try {
      const build = await fresh()
      // Cold: per tier (the base and its two runs) one index GET (the fixture's index spans merge within a tier), then
      // its pages: 2 rounds, then 6 whole groups. Before the pipelining: the same GETs, the same rounds, with a barrier.
      expect(await request(build)).toEqual({ 'pindex-gets': 3, 'pindex-held': 0, 'pindex-colo': 0, 'pdata-gets': 3, pgroups: 5, pgets: 6, prounds: 2, files: 12 })
      // The same isolate again: every index held, the pages are the only GETs (1 round; the whole groups are held
      // decoded, so one group fewer is a paged candidate). Before, the 3 index GETs ran again, a round ahead of the pages.
      expect(await request(build)).toEqual({ 'pindex-gets': 0, 'pindex-held': 5, 'pindex-colo': 0, 'pdata-gets': 3, pgroups: 5, pgets: 3, prounds: 1, files: 3 })
      // A new isolate in the same colo: every index from the colo's copies (the 6th group's too, read whole again), no
      // index GET: 1 round of pages, then the whole groups.
      expect(await request(await fresh())).toEqual({ 'pindex-gets': 0, 'pindex-held': 0, 'pindex-colo': 6, 'pdata-gets': 3, pgroups: 5, pgets: 3, prounds: 1, files: 9 })
    } finally {
      g.caches = prev
      vi.resetModules()
    }
  })

  describe('from the level index (`interval-store-level/`: the same store built with `build -l`, its runs carrying it)', () => {
    const levelEnv = async (reads: string[]) => {
      const { db } = await sqliteD1()
      return { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: 'interval-store-level', INDEX_R2: r2('iv-churn', reads) } as Env
    }
    const traced = () => {
      const t: Record<string, number> = {}
      return { t, trace: (name: string, v: number) => { t[name] = (t[name] ?? 0) + v } }
    }

    it('answers every directory as the scans\' files do, reading the level index only', async () => {
      const lv = await levelEnv([])
      const got = []
      for (const c of cases) {
        const { t, trace } = traced()
        const l = (await buildLevels(lv, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false, trace })).get(c.path)!
        expect([c.from, c.to, c.path, t['level-index']]).toEqual([c.from, c.to, c.path, 1])
        got.push(shape(l))
      }
      expect(got).toEqual(cases.map(wanted))
    })

    it('answers a batch of directories as each alone, and follows chains', async () => {
      const lv = await levelEnv([])
      for (const pr of [...new Set(cases.map(c => `${c.from}|${c.to}`))]) {
        const [from, to] = pr.split('|')
        const dirs = cases.filter(c => c.from === from && c.to === to).map(c => c.path)
        const batch = await buildLevels(lv, { from, to, dirs, k: 512, m: 'churn', chain: false })
        expect(dirs.map(d => shape(batch.get(d)!))).toEqual(dirs.map(d => wanted(cases.find(c => c.from === from && c.to === to && c.path === d)!)))
      }
      const c = cases.find(x => x.path === '' && x.rows.length === 1 && x.rows[0].k === 'dir')!
      const got = await buildLevels(lv, { from: c.from, to: c.to, dirs: [''], k: 512, m: 'churn' })
      expect(got.get('')!.next).toBe(c.rows[0].p)
      expect(shape(got.get(c.rows[0].p)!)).toEqual(wanted(cases.find(x => x.from === c.from && x.to === c.to && x.path === c.rows[0].p)!))
    })

    // The files holding a scan of `(from, to]`: the base (scans to 2026-08-03) and the newest manifest's two runs.
    const touched = (from: string, to: string): string[] => [
      ...(from < '2026-08-03' ? ['served/level.parquet'] : []),
      ...(from < '2026-08-05' && to >= '2026-08-04' ? ['deltas/2026-08-04_2026-08-05/served/level.parquet'] : []),
      ...(to >= '2026-08-06' ? ['deltas/2026-08-06/served/level.parquet'] : []),
    ].map(k => `interval-store-level/g/${k}`)

    it.each([
      ['one directory', ['b1']],
      ['a batch of three (siblings b1, b2, and b1/x)', ['b1', 'b2', 'b1/x']],
    ])('reads %s with one run per touched file in each of two waves, and no other data', async (_, dirs) => {
      const reads: string[] = []
      const lv = await levelEnv(reads)
      const from = '2026-08-02', to = '2026-08-06'
      // No page index held; what's counted is the request's data reads.
      clearIsolate()
      const { t, trace } = traced()
      const got = await buildLevels(lv, { from, to, dirs, k: 512, m: 'churn', chain: false, trace })
      expect(dirs.map(d => shape(got.get(d)!))).toEqual(dirs.map(d => wanted(cases.find(c => c.from === from && c.to === to && c.path === d)!)))
      const files = touched(from, to).sort()
      // Wave 1 (page indexes), then wave 2 (pages): each file once per wave (its dirs' ranges are near: one run), every
      // run of a wave on one limiter, so each wave is ⌈runs / RUN_READS⌉ rounds; nothing else is read.
      const data = reads.filter(k => k.endsWith('.parquet') && !k.endsWith('.groups.parquet'))
      expect([data.slice(0, files.length).sort(), data.slice(files.length).sort()]).toEqual([files, files])
      expect([t['lv-index-gets'], t['lv-data-gets'], t['lv-index-rounds'], t['lv-data-rounds']]).toEqual([files.length, files.length, 1, 1])
      expect(t['lv-groups']).toBeGreaterThan(files.length) // several groups per file, still one run each
    })

    describe('the page indexes it holds', () => {
      const from = '2026-08-02', to = '2026-08-06', dirs = ['b1', 'b2', 'b1/x']
      const IX = ['lv-index-held', 'lv-index-colo', 'lv-index-joined', 'lv-index-miss', 'lv-index-gets', 'lv-index-rounds', 'lv-data-gets'] as const
      const answers = () => dirs.map(d => wanted(cases.find(c => c.from === from && c.to === to && c.path === d)!))
      const dataReads = (log: string[]) => log.filter(k => k.endsWith('/served/level.parquet')).length
      /** Run `f` with a colo kept across its reads (`match`es counted). */
      const withColo = async <T>(f: (c: { puts: () => number; matches: () => number }) => Promise<T>): Promise<T> => {
        const held = new Map<string, ArrayBuffer>()
        let matches = 0
        const g = globalThis as unknown as { caches: unknown }
        const prev = g.caches
        g.caches = { default: {
          match: async (r: Request) => { matches++; return held.has(r.url) ? new Response(held.get(r.url)!.slice(0)) : undefined },
          put: async (r: Request, res: Response) => { held.set(r.url, await res.arrayBuffer()) },
        } }
        try {
          return await f({ puts: () => [...held.keys()].filter(u => u.includes('?pi=')).length, matches: () => matches })
        } finally {
          g.caches = prev
        }
      }
      /** One read through `build` (a module graph's, an isolate's): its answers, its counters, its data file reads. */
      const read = async (build: typeof buildLevels, lv: Env, log: string[]) => {
        const { t, trace } = traced()
        const at = log.length
        const got = await build(lv, { from, to, dirs, k: 512, m: 'churn', chain: false, trace })
        expect(dirs.map(d => shape(got.get(d)!))).toEqual(answers())
        return { ...Object.fromEntries(IX.map(k => [k, t[k]])), files: dataReads(log.slice(at)), groups: t['lv-groups'] } as Record<string, number> & { files: number; groups: number }
      }
      /** Colo puts landed, those a scope's commit started (not awaited there) too. */
      const settled = async () => {
        await coloPutsSettled()
        await new Promise(r => setTimeout(r, 5))
      }
      /** The fixture's read, cold and alone: every group a miss, one index GET per touched file, then one data GET each. */
      const COLD = (groups: number) => ({ 'lv-index-held': 0, 'lv-index-colo': 0, 'lv-index-joined': 0, 'lv-index-miss': groups, 'lv-index-gets': 3, 'lv-index-rounds': 1, 'lv-data-gets': 3, files: 6, groups })
      /** The same read with every index held: no index GET at all. */
      const WARM = (groups: number, how: 'lv-index-held' | 'lv-index-colo') => ({ ...COLD(groups), 'lv-index-miss': 0, 'lv-index-gets': 0, 'lv-index-rounds': 0, files: 3, [how]: groups })

      it('a second read of the same groups issues no index GET; a new isolate in the colo neither; answers unchanged', () => withColo(async c => {
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        const first = await read(buildLevels, lv, log)
        const n = first.groups
        expect(n).toBeGreaterThan(3)
        expect(first).toEqual(COLD(n))
        await settled()
        expect(c.puts()).toBe(n)
        // The same isolate: not even a colo lookup.
        const looked = c.matches()
        expect(await read(buildLevels, lv, log)).toEqual(WARM(n, 'lv-index-held'))
        expect(c.matches()).toBe(looked)
        // A new isolate (a fresh module graph), the same colo.
        vi.resetModules()
        const other = (await import('./diffLevel')).buildLevels
        expect(await read(other, lv, log)).toEqual(WARM(n, 'lv-index-colo'))
        expect(await read(other, lv, log)).toEqual(WARM(n, 'lv-index-held'))
        vi.resetModules()
      }))

      it('a colo copy of another length (another layout) is a miss, and is replaced', () => withColo(async c => {
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        const g = globalThis as unknown as { caches: { default: { match: (r: Request) => Promise<Response | undefined> } } }
        const real = g.caches.default.match
        g.caches.default.match = async r => (r.url.includes('?pi=') ? new Response(new ArrayBuffer(3)) : real(r))
        const { groups } = await read(buildLevels, lv, log).then(r => { expect(r).toEqual(COLD(r.groups)); return r })
        await settled()
        expect(c.puts()).toBe(groups)
      }))

      it('concurrent reads share one index wave: the second joins every group the first is reading', () => withColo(async () => {
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        // Whichever gets there first.
        const [a, b] = (await Promise.all([read(buildLevels, lv, log), read(buildLevels, lv, log)])).sort((x, y) => y['lv-index-miss'] - x['lv-index-miss'])
        const n = a.groups
        // The log is shared, so each read's `files` is whatever had landed by its end.
        expect({ ...a, files: 0 }).toEqual({ ...COLD(n), files: 0 })
        expect({ ...b, files: 0 }).toEqual({ ...COLD(n), 'lv-index-joined': n, 'lv-index-miss': 0, 'lv-index-gets': 0, 'lv-index-rounds': 0, files: 0 })
        // Together: one index wave (3 GETs), each read's own data wave.
        expect(dataReads(log)).toBe(3 + 3 + 3)
      }))

      it('no read joins a provisional read: beside it, a read reads every index itself', () => withColo(async () => {
        await loadScopes()
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        // The scope's index wave stops at its 3 GETs, its entries in flight, until the other read is done.
        const real = lv.INDEX_R2!
        let stopped = 0
        let go = () => {}
        const release = new Promise<void>(r => { go = r })
        lv.INDEX_R2 = { ...real, get: async (k: string, o?: never) => {
          if (k.endsWith('/served/level.parquet') && stopped < 3) {
            stopped++
            await release
          }
          return real.get(k, o)
        } } as unknown as R2Bucket
        const early = ahead({}, true)
        const inScope = early.later(() => read(buildLevels, lv, log))()
        await vi.waitFor(() => expect(stopped).toBe(3))
        const plain = await read(buildLevels, lv, log)
        go()
        const scoped = await inScope
        early.done()
        expect(plain).toEqual(COLD(scoped.groups))
      }))

      it('a provisional read refused at its gate leaves nothing: no held bytes, no colo copy', () => withColo(async c => {
        await loadScopes()
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        const refused = ahead({}, true)
        const { groups } = await refused.later(() => read(buildLevels, lv, log))()
        refused.done()
        await settled()
        expect(c.puts()).toBe(0)
        expect(await read(buildLevels, lv, log)).toEqual(COLD(groups))
      }))

      it('a provisional read whose gate passes lands what it read', () => withColo(async c => {
        await loadScopes()
        const log: string[] = []
        const lv = await levelEnv(log)
        clearIsolate()
        const early = ahead({}, true)
        const first = await early.later(() => read(buildLevels, lv, log))()
        expect(first).toEqual(COLD(first.groups))
        early.commit()
        early.done()
        await settled()
        expect(c.puts()).toBe(first.groups)
        expect(await read(buildLevels, lv, log)).toEqual(WARM(first.groups, 'lv-index-held'))
      }))
    })

    it('its read laps every phase, each round in order, wherever a directory has no transition', async () => {
      const lv = await levelEnv([])
      const laps: string[] = []
      const c = cases.find(c => c.path === '')!
      await buildLevels(lv, { from: c.from, to: c.to, dirs: [''], k: 512, m: 'churn', chain: false, lap: n => { laps.push(n) } })
      expect(laps).toEqual(['churn', 'plan', 'index', 'pages', 'data', 'decode', 'rollup', 'own'])
    })

    describe('its budget', () => {
      const whole = LEVEL_BUDGET.bytes
      afterEach(() => { LEVEL_BUDGET.bytes = whole })
      const span = () => cases.find(x => x.from === '2026-08-02' && x.to === '2026-08-06' && x.path === '')!
      const read = async (lv: Env, dirs: string[]) => {
        const c = span()
        const o = { from: c.from, to: c.to, dirs, k: 512, m: 'churn' as const, chain: false, more: [] as string[], wide: [] as string[] }
        const got = await buildLevels(lv, o)
        return { levels: new Map([...got].map(([p, l]) => [p, shape(l)])), more: o.more, wide: o.wide }
      }
      /** Two directories this store has, with their rows' charge in bytes as read alone. */
      /** A file with a closing transition in some range: asked as a directory, the level read returns null for its round. */
      const closingFile = async (lv: Env) => {
        for (const x of cases) {
          for (const r of x.rows.filter(r => r.k === 'file' && r.ob === 0 && r.oa > 0)) {
            const { t, trace } = traced()
            await buildLevels(lv, { from: x.from, to: x.to, dirs: [r.p], k: 512, m: 'churn', chain: false, trace })
            if (t['level-index'] === undefined) return { from: x.from, to: x.to, path: r.p }
          }
        }
        throw new Error('the fixture has no file closing in a range')
      }
      const dirsOf = () => [...new Set(cases.filter(x => x.from === span().from && x.to === span().to && x.path !== '').map(x => x.path))].slice(0, 2)

      it('a request past it answers what fits and names the rest: asking for those finishes it, as a whole read answers', async () => {
        const lv = await levelEnv([])
        const dirs = dirsOf()
        expect(dirs.length).toBe(2)
        const all = await read(lv, dirs)
        expect([[...all.levels.keys()].sort(), all.more, all.wide]).toEqual([[...dirs].sort(), [], []])
        const { t, trace } = traced()
        const c = span()
        await buildLevels(lv, { from: c.from, to: c.to, dirs: [dirs[0]], k: 512, m: 'churn', chain: false, trace })
        expect(t['lv-charge']).toBeGreaterThan(0)
        // Room for the first directory's rows, exactly: the second is left for another request.
        LEVEL_BUDGET.bytes = t['lv-charge']
        const first = await read(lv, dirs)
        expect([[...first.levels.keys()], first.more, first.wide]).toEqual([[dirs[0]], [dirs[1]], []])
        LEVEL_BUDGET.bytes = whole
        const rest = await read(lv, first.more)
        expect(new Map([...first.levels, ...rest.levels])).toEqual(all.levels)
      })

      it('requests in one isolate share it: concurrent reads past it leave the later one `more`, and the charges come back', async () => {
        const lv = await levelEnv([])
        const dirs = dirsOf()
        const { t, trace } = traced()
        const c = span()
        await buildLevels(lv, { from: c.from, to: c.to, dirs: [dirs[0]], k: 512, m: 'churn', chain: false, trace })
        expect(levelHeldBytes()).toBe(0)
        LEVEL_BUDGET.bytes = t['lv-charge']
        const [a, b] = await Promise.all([read(lv, [dirs[0]]), read(lv, [dirs[0]])])
        const got = [a, b].map(r => [[...r.levels.keys()], r.more, r.wide])
        expect(got.sort((x, y) => x[0].length - y[0].length)).toEqual([[[], [dirs[0]], []], [[dirs[0]], [], []]])
        expect(levelHeldBytes()).toBe(0)
        const again = await read(lv, [dirs[0]])
        expect([[...again.levels.keys()], again.more, again.wide]).toEqual([[dirs[0]], [], []])
      })

      it('a charge a cancelled request never released expires', async () => {
        const lv = await levelEnv([])
        const dirs = dirsOf()
        const { t, trace } = traced()
        const c = span()
        await buildLevels(lv, { from: c.from, to: c.to, dirs: [dirs[0]], k: 512, m: 'churn', chain: false, trace })
        LEVEL_BUDGET.bytes = t['lv-charge']
        chargeLevel({ left: LEVEL_BUDGET.bytes }, LEVEL_BUDGET.bytes)
        const busy = await read(lv, [dirs[0]])
        expect([[...busy.levels.keys()], busy.more]).toEqual([[], [dirs[0]]])
        const now = Date.now
        Date.now = () => now() + 121_000
        try {
          expect(levelHeldBytes()).toBe(0)
        } finally {
          Date.now = now
        }
      })

      it('a round the level index can\'t answer: the `path` sort answers afresh, and nothing the rounds set aside still stands', async () => {
        const lv = await levelEnv([])
        // A file asked as a directory: its own closing shows it a file, so its round falls back (`level-index` unset).
        const file = await closingFile(lv)
        const big = 'b1/x'
        const { t, trace } = traced()
        await buildLevels(lv, { from: file.from, to: file.to, dirs: [file.path], k: 512, m: 'churn', chain: false, trace })
        expect(t['level-index']).toBeUndefined()
        const charge = t['lv-charge']
        const seen = traced()
        await buildLevels(lv, { from: file.from, to: file.to, dirs: [big], k: 512, m: 'churn', chain: false, trace: seen.trace })
        expect(seen.t['lv-charge']).toBeGreaterThan(charge)
        // `big` is too wide alone, set aside as `wide`; then the file's round is null. The path sort reads both.
        LEVEL_BUDGET.bytes = charge + (seen.t['lv-charge'] - charge) / 2
        const o = { from: file.from, to: file.to, dirs: [big, file.path], k: 512, m: 'churn' as const, chain: false, more: [] as string[], wide: [] as string[] }
        const got = await buildLevels(lv, o)
        const answered = [...got.keys()]
        expect([...answered, ...o.more, ...o.wide].sort()).toEqual([big, file.path].sort())
        expect(answered.filter(p => o.more.includes(p) || o.wide.includes(p))).toEqual([])
        expect(levelHeldBytes()).toBe(0)
      })

      it('a directory that fits nowhere: a request of it alone is too wide, beside others it is named `wide`, and nothing is cached for it', async () => {
        const lv = await levelEnv([])
        const dirs = dirsOf()
        LEVEL_BUDGET.bytes = 1
        await expect(read(lv, [dirs[0]])).rejects.toThrow(/^query too wide/)
        const got = await read(lv, dirs)
        expect([[...got.levels.keys()], got.more, [...got.wide].sort()]).toEqual([[], [], [...dirs].sort()])
      })
    })

    describe('children whose last segment is empty, named \'/\' in the level sort', () => {
      const oddEnv = async (store: string) => {
        const { db } = await sqliteD1()
        return { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INTERVAL_STORE_PREFIX: store, INDEX_R2: r2('iv-churn', []) } as Env
      }
      const answer = async (lv: Env, from: string, to: string, dir: string, given?: { level: null }) => {
        const { t, trace } = traced()
        const l = (await buildLevels(lv, { from, to, dirs: [dir], k: 512, m: 'churn', chain: false, trace }, given)).get(dir)!
        return { l: shape(l), index: t['level-index'] ?? 0 }
      }
      const pair = async (lv: Env, from: string, to: string, dir: string) => {
        const [lvl, path] = [await answer(lv, from, to, dir), await answer(lv, from, to, dir, { level: null })]
        return { dir, from, to, index: lvl.index, same: lvl.l, want: path.l }
      }

      describe('a directory `b1/x/` under `b1/x` (`interval-store-slash/`: a key `b1/x//k.bin` from 08-05 on)', () => {
        it('where it changes in the range, the level index answers as the `path` sort does: its child is `b1/x/`, not `b1/x`\'s own', async () => {
          const lv = await oddEnv('interval-store-slash')
          const { l, index } = await answer(lv, '2026-08-04', '2026-08-05', 'b1/x')
          const path = await answer(lv, '2026-08-04', '2026-08-05', 'b1/x', { level: null })
          expect(l).toEqual(path.l)
          expect(index).toBe(1)
          expect([l.bytes_before, l.bytes_after, l.objects_before, l.objects_after, l.churn]).toEqual([100, 47, 4, 5, 67])
          expect(l.rows.filter(r => r.name === '')).toEqual([{ name: '', kind: 'dir', bytes_before: 0, bytes_after: 7, objects_before: 0, objects_after: 1, churn: 7, status: 'added' }])
        })

        it('where it doesn\'t, the level index still answers, as the `path` sort does', async () => {
          const lv = await oddEnv('interval-store-slash')
          const { l, index } = await answer(lv, '2026-08-05', '2026-08-06', 'b1/x')
          const path = await answer(lv, '2026-08-05', '2026-08-06', 'b1/x', { level: null })
          expect(index).toBe(1)
          expect(l).toEqual(path.l)
        })
      })

      describe('objects named for a directory, runs of slashes and leading slashes (`interval-store-placeholder/`)', () => {
        const DATES = ['2026-08-02', '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06']
        const DIRS = ['', 'b1', 'b1/x', 'b1/x/y', 'b1/w', 'b1/w/', 'b1/w//', 'b2', 'b2/q', 'b2/z', 'b2/z/', 'b2/z//', '/', '/lead']

        it('a placeholder `b1/x/` is an ordinary child of `b1/x`: the level index answers, with its row', async () => {
          const lv = await oddEnv('interval-store-placeholder')
          const { l, index } = await answer(lv, '2026-08-04', '2026-08-05', 'b1/x')
          expect(index).toBe(1)
          expect(l.rows.filter(r => r.name === '')).toEqual([{ name: '', kind: 'file', bytes_before: 0, bytes_after: 0, objects_before: 0, objects_after: 1, churn: 0, status: 'added' }])
        })

        it('one gaining bytes, and one in the base, the same', async () => {
          const lv = await oddEnv('interval-store-placeholder')
          const grown = await answer(lv, '2026-08-05', '2026-08-06', 'b1/x')
          const based = await answer(lv, '2026-08-02', '2026-08-03', 'b2/q')
          expect([grown.index, based.index]).toEqual([1, 1])
          expect(grown.l.rows.filter(r => r.name === '')).toEqual([{ name: '', kind: 'file', bytes_before: 0, bytes_after: 3, objects_before: 1, objects_after: 1, churn: 3, status: 'changed' }])
          expect(based.l.rows.filter(r => r.name === '')).toEqual([{ name: '', kind: 'file', bytes_before: 0, bytes_after: 0, objects_before: 0, objects_after: 1, churn: 0, status: 'added' }])
        })

        it('every directory over every window: what the `path` sort answers, from the level index', async () => {
          const lv = await oddEnv('interval-store-placeholder')
          for (let i = 0; i < DATES.length; i++) {
            for (let j = i + 1; j < DATES.length; j++) {
              for (const dir of DIRS) {
                const got = await pair(lv, DATES[i], DATES[j], dir)
                expect({ dir, from: got.from, to: got.to, index: got.index, l: got.same }).toEqual({ dir, from: got.from, to: got.to, index: 1, l: got.want })
              }
            }
          }
        })

        it('the root\'s child \'\' (a key with a leading slash), its own `/` and `/lead` below it', async () => {
          const lv = await oddEnv('interval-store-placeholder')
          const root = await pair(lv, '2026-08-04', '2026-08-05', '')
          expect(root.same.rows.filter(r => r.name === '')).toEqual([{ name: '', kind: 'dir', bytes_before: 0, bytes_after: 11, objects_before: 0, objects_after: 2, churn: 11, status: 'added' }])
          expect(root.same).toEqual(root.want)
        })
      })
    })

    it('reads the path sort where a touched file lacks the level index', async () => {
      // `interval-store/` has no level sort: the same answers, by the old read.
      const { t, trace } = traced()
      const c = cases[0]
      const l = (await buildLevels(env, { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn', chain: false, trace })).get(c.path)!
      expect([shape(l), t['level-index']]).toEqual([wanted(c), undefined])
    })
  })

  it('reads only the groups whose versions change in the range', async () => {
    let fewer = 0
    for (const c of cases) {
      const e = asOfScans(env, [c.from, c.to])
      const [hA, hB] = await Promise.all([openIndex(e, c.from, 'path'), openIndex(e, c.to, 'path')])
      const h = withChurn(hB)!
      const d = c.path === '' ? 0 : c.path.split('/').length
      const rect: Rect = { dLo: d + 1, dHi: d + 1, pLo: c.path ? `${c.path}/` : '', pHi: c.path ? `${c.path}0` : '￿' }
      const range = { ...h, historyRange: [hA.asOf!, hB.asOf!] as [number, number], runs: h.runs?.map(r => ({ ...r, historyRange: [hA.asOf!, hB.asOf!] as [number, number] })) }
      const all = await planRects(range, [rect])
      const { spans } = await planChanged(h, [rect], hA.asOf!, hB.asOf!)
      expect(spans.every(s => all.some(x => x.rg === s.rg && (x.tier ?? 0) === (s.tier ?? 0)))).toBe(true)
      if (spans.length < all.length) fewer++
    }
    expect(fewer).toBeGreaterThan(0)
  })
})
