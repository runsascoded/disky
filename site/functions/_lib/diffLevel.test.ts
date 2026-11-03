import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { buildLevels, type Level, levelK, levelOf, type LevelRow, mergeLevels } from './diffLevel'
import { asOfScans, openIndex, planChanged, planRects, type Rect, withChurn } from './index'
import { sqliteD1 } from './testD1'
import { fixtureR2 as r2 } from './testR2'
import { readJson } from './testStore'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// `fixtures/iv-churn/`: a churn-carrying interval store (a base over four scans, three appended as runs, merged by the
// carries), 4 rows per group (`gen.py`). `expected.json`: per `(from, to, path)` from the churn base on, the level
// answer computed from the scans' files alone.

type Want = { from: string; to: string; path: string; a: number; b: number; oa: number; ob: number; churn: number; rows: { p: string; k: 'file' | 'dir'; a: number; b: number; oa: number; ob: number; churn: number }[] }

/** The oracle's answer in the level's shape: rows by name under the path, each with its status. */
function wanted(w: Want): Pick<Level, 'path' | 'a' | 'b' | 'oa' | 'ob' | 'churn' | 'rows' | 'unchanged'> {
  const rows = w.rows.map(({ p, k, a, b, oa, ob, churn }): LevelRow => ({
    p: w.path ? p.slice(w.path.length + 1) : p, k, a, b, oa, ob, churn,
    s: !oa && !ob ? 'changed' : !oa ? 'added' : !ob ? 'removed' : 'changed',
  }))
  const sum = (f: 'a' | 'b' | 'oa' | 'ob') => rows.reduce((n, r) => n + r[f], 0)
  return { path: w.path, a: w.a, b: w.b, oa: w.oa, ob: w.ob, churn: w.churn, rows: byName(rows), unchanged: { a: w.a - sum('a'), b: w.b - sum('b'), oa: w.oa - sum('oa'), ob: w.ob - sum('ob') } }
}
const byName = (rows: LevelRow[]): LevelRow[] => [...rows].sort((x, y) => (x.p < y.p ? -1 : 1))
const shape = (l: Level) => ({ path: l.path, a: l.a, b: l.b, oa: l.oa, ob: l.ob, churn: l.churn, rows: byName(l.rows), unchanged: l.unchanged })

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
    const own = { a: 100, b: 100, oa: 10, ob: 10, churn: 60 }
    const kid = (p: string, a: number, b: number, churn: number) => ({ p: `d/${p}`, k: 'file' as const, s: 'changed' as const, a, b, oa: 1, ob: 1, churn })
    const kids = [kid('x', 10, 10, 30), kid('y', 10, 30, 20), kid('z', 5, 0, 5), kid('w', 7, 7, 0)]
    const byChurn = levelOf('d', own, kids, 2, 'churn')
    expect([byChurn.rows.map(r => r.p), byChurn.other, byChurn.unchanged]).toEqual([
      ['x', 'y'],
      { n: 1, a: 5, b: 0, oa: 1, ob: 1, churn: 5 },
      { a: 75, b: 60, oa: 7, ob: 7 },
    ])
    expect(levelOf('d', own, kids, 2, 'net').rows.map(r => r.p)).toEqual(['y', 'z'])
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
    expect(want.some(l => l.other?.n === 1)).toBe(true)
    // Below the root, a directory's answer is its own store's.
    for (const c of cases.filter(x => x.path.startsWith('b1/'))) {
      const o = { from: c.from, to: c.to, dirs: [c.path], k: 512, m: 'churn' as const, chain: false }
      expect((await buildLevels(b1, o)).get(c.path)).toEqual((await buildLevels(env, o)).get(c.path))
    }
  })

  it('merges by the measure, ranking and cutting as `levelOf` does', () => {
    const kid = (p: string, a: number, b: number, churn: number) => ({ p, k: 'dir' as const, s: 'changed' as const, a, b, oa: 1, ob: 1, churn })
    const kids = [kid('x', 10, 10, 30), kid('y', 10, 30, 20), kid('z', 5, 0, 5), kid('w', 7, 7, 0), kid('v', 1, 2, 1)]
    const sum = (ks: typeof kids, extra: number) => ({ a: ks.reduce((n, c) => n + c.a, extra), b: ks.reduce((n, c) => n + c.b, extra), oa: ks.length + extra, ob: ks.length + extra, churn: ks.reduce((n, c) => n + c.churn, 0) })
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
