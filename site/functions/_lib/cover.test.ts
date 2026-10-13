import { describe, expect, it } from 'vitest'
import { coverFloor, coverSet, type CoverItem, type CoverRoot, depthOf, isFull, type Lookup, type PathTotal } from './cover.js'

/** A fixture store: object paths → bytes. Folders are every proper prefix of an object path. */
type Store = Record<string, number>

const parentOf = (p: string) => p.slice(0, Math.max(0, p.lastIndexOf('/')))
const under = (p: string, a: string) => a === '' || p === a || p.startsWith(a + '/')

/** Every path's totals and kind, from the objects. */
function totals(s: Store): Map<string, PathTotal> {
  const t = new Map<string, PathTotal>()
  for (const [p, b] of Object.entries(s)) {
    t.set(p, { b, o: 1, kind: 'file' })
    for (let a = parentOf(p); a; a = parentOf(a)) {
      const x = t.get(a) ?? { b: 0, o: 0, kind: 'dir' as const }
      t.set(a, { ...x, b: x.b + b, o: x.o + 1 })
    }
  }
  return t
}

/** The filter's match roots under `view`: outermost paths whose last segment holds `q` (the view itself never). */
function matchRoots(s: Store, q: string, view: string): CoverRoot[] {
  const t = totals(s)
  const hit = (p: string) => p.split('/').pop()!.toLowerCase().includes(q)
  const roots = [...t.keys()].filter(p => p !== view && under(p, view) && hit(p))
    .filter(p => { for (let a = parentOf(p); a && a !== view; a = parentOf(a)) if (hit(a)) return false; return true })
  return roots.sort().map(p => ({ path: p, b: t.get(p)!.b, o: t.get(p)!.o }))
}

/** A lookup over the fixture, counting calls and optionally refusing past `budget` paths. */
function lookupOf(s: Store, budget = Infinity): Lookup & { calls: string[][] } {
  const t = totals(s)
  let used = 0
  const f = (async (paths: string[]) => {
    f.calls.push(paths)
    used += paths.length
    if (used > budget) return null
    return new Map(paths.flatMap(p => t.has(p) ? [[p, t.get(p)!] as const] : []))
  }) as Lookup & { calls: string[][] }
  f.calls = []
  return f
}

/** Brute force, independent of the sums: every prefix (folder or object) all of whose objects match, at
 *  depth ≥ the floor or inside a match root; the outermost of those. */
function bruteCover(s: Store, q: string, view: string, minDepth: number): CoverItem[] {
  const t = totals(s)
  const roots = matchRoots(s, q, view)
  const matched = new Set(Object.keys(s).filter(p => roots.some(r => under(p, r.path))))
  const floor = Math.max(minDepth, depthOf(view))
  const inRoot = (p: string) => roots.some(r => under(p, r.path))
  const exact = [...t.keys()].filter(p => under(p, view) && (p !== view || view !== '')
    && Object.keys(s).filter(o => under(o, p)).every(o => matched.has(o))
    && (depthOf(p) >= floor || inRoot(p)))
  const outer = exact.filter(p => !exact.some(a => a !== p && under(p, a)))
  return outer.sort().map(p => ({
    path: p, kind: t.get(p)!.kind, b: t.get(p)!.b, o: t.get(p)!.o,
    roots: roots.filter(r => under(r.path, p)).length,
  }))
}

/** The objects a cover's items take. */
const taken = (s: Store, items: CoverItem[]) => Object.keys(s).filter(o => items.some(i => under(o, i.path))).sort()

// Two buckets. `tomat` matches: a whole folder of matching files (`b/clips` → collapses), a folder holding
// one matching and one other file (`b/mixed` → stays per file), a nested full folder (`b/deep/x/y` all
// match → `b/deep` too), a zero-byte non-match beside matching files (`b/zero`: bytes equal, objects not),
// a folder named for the term (`c/tomato` → a root itself), and a one-object folder root (`c/tomat_one`).
const FIX: Store = {
  'b/clips/a_tomato.mp3': 10, 'b/clips/b_tomato.mp3': 20,
  'b/mixed/tomato.txt': 5, 'b/mixed/other.txt': 7,
  'b/deep/x/y/tomat1': 3, 'b/deep/x/y/tomat2': 4, 'b/deep/x/tomat3': 1,
  'b/zero/tomat.a': 9, 'b/zero/empty': 0,
  'b/other/nothing': 100,
  'c/tomato/a': 1, 'c/tomato/b': 2,
  'c/tomat_one/z': 6,
  'c/keep/k': 1,
}

describe('coverSet', () => {
  it('collapses fully matched folders, keeps partly matched ones per root, and places one-object roots', async () => {
    const got = await coverSet(matchRoots(FIX, 'tomat', ''), '', lookupOf(FIX), { minDepth: 2 })
    expect(got.items).toEqual([
      { path: 'b/clips', kind: 'dir', b: 30, o: 2, roots: 2 },
      { path: 'b/deep', kind: 'dir', b: 8, o: 3, roots: 3 },
      { path: 'b/mixed/tomato.txt', kind: 'file', b: 5, o: 1, roots: 1 },
      { path: 'b/zero/tomat.a', kind: 'file', b: 9, o: 1, roots: 1 },
      { path: 'c/tomat_one', kind: 'dir', b: 6, o: 1, roots: 1 },
      { path: 'c/tomato', kind: 'dir', b: 3, o: 2, roots: 1 },
    ])
    // `b/clips`, `b/mixed`, `b/deep`, `b/deep/x`, `b/deep/x/y`, `b/zero` (`c`'s are under the floor).
    expect([got.looked, got.unchecked]).toEqual([6, 0])
  })

  it('equals brute force on the fixture at every view and floor', async () => {
    const views = ['', 'b', 'b/deep', 'b/deep/x', 'c', 'b/mixed']
    for (const view of views) for (const minDepth of [1, 2, 3]) {
      const roots = matchRoots(FIX, 'tomat', view)
      const got = await coverSet(roots, view, lookupOf(FIX), { minDepth })
      expect([view, minDepth, got.items]).toEqual([view, minDepth, bruteCover(FIX, 'tomat', view, minDepth)])
    }
  })

  it('equals brute force on random trees, and takes exactly the matched objects', async () => {
    let seed = 7
    const rnd = (n: number) => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed % n }
    const names = ['tomat', 'tomato_x', 'a', 'b', 'c', 'xtomatx', 'd']
    for (let trial = 0; trial < 200; trial++) {
      const s: Store = {}
      for (let i = 0, n = 1 + rnd(14); i < n; i++) {
        const segs = ['bk' + rnd(2), ...Array.from({ length: 1 + rnd(4) }, () => names[rnd(names.length)])]
        const p = segs.join('/')
        // An object's path is never a folder's.
        if (Object.keys(s).some(o => o.startsWith(p + '/') || p.startsWith(o + '/'))) continue
        s[p] = rnd(3) === 0 ? 0 : 1 + rnd(50)
      }
      const roots = matchRoots(s, 'tomat', '')
      const got = await coverSet(roots, '', lookupOf(s), { minDepth: 2 })
      expect(got.items).toEqual(bruteCover(s, 'tomat', '', 2))
      expect(taken(s, got.items)).toEqual(Object.keys(s).filter(o => roots.some(r => under(o, r.path))).sort())
    }
  })

  it('mutation: a bytes-only fullness test is caught (it covers the zero-byte non-match)', async () => {
    const bytesOnly = (m: { b: number }, t: { b: number }) => m.b === t.b
    const roots = matchRoots(FIX, 'tomat', '')
    const bad = await coverSet(roots, '', lookupOf(FIX), { minDepth: 2, full: bytesOnly })
    expect(bad.items).not.toEqual(bruteCover(FIX, 'tomat', '', 2))
    expect(taken(FIX, bad.items).filter(o => !roots.some(r => under(o, r.path)))).toEqual(['b/zero/empty'])
    // …and the real test isn't.
    const good = await coverSet(roots, '', lookupOf(FIX), { minDepth: 2, full: isFull })
    expect(taken(FIX, good.items).filter(o => !roots.some(r => under(o, r.path)))).toEqual([])
  })

  it('never collapses above the view or the floor', async () => {
    const s: Store = { 'b/x/tomat1': 1, 'b/x/tomat2': 2 }
    const roots = matchRoots(s, 'tomat', 'b/x')
    expect((await coverSet(roots, 'b/x', lookupOf(s), { minDepth: 1 })).items).toEqual([
      { path: 'b/x', kind: 'dir', b: 3, o: 2, roots: 2 },
    ])
    expect((await coverSet(matchRoots(s, 'tomat', ''), '', lookupOf(s), { minDepth: 3 })).items.map(i => i.path)).toEqual(['b/x/tomat1', 'b/x/tomat2'])
  })

  // The bulk bar's drilled views: a view folder (or a non-matching-name ancestor) every object of which
  // matches collapses to itself; one level up, only the fully matching children do.
  const SHOWS: Store = {
    'b/clips/A/x_tomato_1.mp3': 10, 'b/clips/A/x_tomato_2.mp3': 20,
    'b/clips/B/s1/tomat_a': 1, 'b/clips/B/s2/tomat_b': 2,
    'b/clips/C/tomato_c': 3, 'b/clips/C/plain': 4,
  }
  it('the view folder itself collapses when every object under it matches', async () => {
    expect((await coverSet(matchRoots(SHOWS, 'tomat', 'b/clips/A'), 'b/clips/A', lookupOf(SHOWS), { minDepth: 2 })).items).toEqual([
      { path: 'b/clips/A', kind: 'dir', b: 30, o: 2, roots: 2 },
    ])
  })
  it('a fully matching ancestor whose own name doesn\'t match collapses (from the view and from above it)', async () => {
    expect((await coverSet(matchRoots(SHOWS, 'tomat', 'b/clips/B'), 'b/clips/B', lookupOf(SHOWS), { minDepth: 2 })).items).toEqual([
      { path: 'b/clips/B', kind: 'dir', b: 3, o: 2, roots: 2 },
    ])
  })
  it('a mixed parent: each fully matching child collapses, the parent doesn\'t', async () => {
    expect((await coverSet(matchRoots(SHOWS, 'tomat', 'b/clips'), 'b/clips', lookupOf(SHOWS), { minDepth: 2 })).items).toEqual([
      { path: 'b/clips/A', kind: 'dir', b: 30, o: 2, roots: 2 },
      { path: 'b/clips/B', kind: 'dir', b: 3, o: 2, roots: 2 },
      { path: 'b/clips/C/tomato_c', kind: 'file', b: 3, o: 1, roots: 1 },
    ])
  })
  it('a view folder holding non-matches stays one item per match (the prod `Adam_Carolla_Show` shape)', async () => {
    const s: Store = { 'b/pod/show/ep_Tomatoes_0': 5, 'b/pod/show/ep_Tomatoes_1': 5, 'b/pod/show/ep_other': 9 }
    expect((await coverSet(matchRoots(s, 'tomat', 'b/pod/show'), 'b/pod/show', lookupOf(s), { minDepth: 2 })).items).toEqual([
      { path: 'b/pod/show/ep_Tomatoes_0', kind: 'file', b: 5, o: 1, roots: 1 },
      { path: 'b/pod/show/ep_Tomatoes_1', kind: 'file', b: 5, o: 1, roots: 1 },
    ])
  })

  it('prunes: an ancestor of a folder holding non-matches is never looked up', async () => {
    const s: Store = { 'b/p/q/tomat': 1, 'b/p/q/other': 1, 'b/p/r/tomat': 1 }
    const look = lookupOf(s)
    await coverSet(matchRoots(s, 'tomat', ''), '', look, { minDepth: 2 })
    // Depth 3 (`b/p/q`, `b/p/r`), then the one-object roots' kinds; `b/p` is poisoned by `b/p/q`.
    // …and `b/p/r` is full, so its root needs no kind of its own.
    expect(look.calls).toEqual([['b/p/q', 'b/p/r'], ['b/p/q/tomat']])
  })

  it('kinds are asked in chunks: a budget cut keeps the ones already placed', async () => {
    const s: Store = { 'b/m/tomat1': 1, 'b/m/tomat2': 2, 'b/m/tomat3': 3, 'b/m/other': 4 }
    const look = lookupOf(s, 3)
    const got = await coverSet(matchRoots(s, 'tomat', ''), '', look, { minDepth: 2, kindChunk: 2 })
    expect([look.calls, got.items.map(i => [i.path, i.kind])]).toEqual([
      [['b/m'], ['b/m/tomat1', 'b/m/tomat2'], ['b/m/tomat3']],
      [['b/m/tomat1', 'file'], ['b/m/tomat2', 'file'], ['b/m/tomat3', null]],
    ])
  })

  it('a kind chunk too wide for one call is halved, not the end of the kinds', async () => {
    const s: Store = Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`b/m${i % 3}/tomat${String(i).padStart(3, '0')}`, 1]).concat([['b/m0/x', 1], ['b/m1/x', 1], ['b/m2/x', 1]]))
    const t = totals(s)
    const sizes: number[] = []
    // A call over 100 paths is too wide; the budget itself never runs out.
    const look: Lookup = async paths => { sizes.push(paths.length); return paths.length > 100 ? 'wide' : new Map(paths.map(p => [p, t.get(p)!] as const)) }
    const got = await coverSet(matchRoots(s, 'tomat', ''), '', look, { minDepth: 2, kindChunk: 256 })
    expect([sizes, got.items.filter(i => i.kind === 'file').length]).toEqual([[3, 256, 128, 64, 64, 64, 64, 44], 300])
  })

  it('over the lookup budget: stays exact, stops collapsing, and leaves unknown kinds unplaced', async () => {
    const roots = matchRoots(FIX, 'tomat', '')
    const got = await coverSet(roots, '', lookupOf(FIX, 3), { minDepth: 2 })
    expect(got.items).toEqual([
      { path: 'b/clips/a_tomato.mp3', kind: null, b: 10, o: 1, roots: 1 },
      { path: 'b/clips/b_tomato.mp3', kind: null, b: 20, o: 1, roots: 1 },
      { path: 'b/deep/x', kind: 'dir', b: 8, o: 3, roots: 3 },
      { path: 'b/mixed/tomato.txt', kind: null, b: 5, o: 1, roots: 1 },
      { path: 'b/zero/tomat.a', kind: null, b: 9, o: 1, roots: 1 },
      { path: 'c/tomat_one', kind: null, b: 6, o: 1, roots: 1 },
      { path: 'c/tomato', kind: 'dir', b: 3, o: 2, roots: 1 },
    ])
    // Depths 4 and 3 fit (`b/deep/x/y`, `b/deep/x`); depth 2's four and the kinds don't.
    expect([got.looked, got.unchecked]).toEqual([2, 4])
    expect(taken(FIX, got.items)).toEqual(Object.keys(FIX).filter(o => roots.some(r => under(o, r.path))).sort())
  })
})

// `coverFloor`: a lower bound on the cover's item count, top-down from the floor, stopping past the cap.
describe('coverFloor', () => {
  // `b/r` holds two matching run dirs, a folder `u` holding a third, and a non-match (not full); `b/s` one
  // matching run dir (full).
  const RUNS: Store = { 'b/r/tomat1/x': 1, 'b/r/tomat2/x': 2, 'b/r/u/tomat3/x': 3, 'b/r/other': 4, 'b/s/tomat4/x': 5 }
  // Five matching run dirs and nothing else: `b/all` is full, the cover is one item for five roots.
  const ALL: Store = Object.fromEntries([1, 2, 3, 4, 5].map(i => [`b/all/tomat${i}/x`, i]))

  it('round 0 reads nothing: the roots at the floor plus the floor folders holding the rest', async () => {
    const roots = matchRoots(FIX, 'tomat', '')
    const look = lookupOf(FIX)
    // `c/tomato`, `c/tomat_one` (depth 2) + `b/clips`, `b/deep`, `b/mixed`, `b/zero` = 6 > 5.
    expect([roots.length, await coverFloor(roots, '', look, { minDepth: 2, cap: 5 }), look.calls]).toEqual([
      9, { n: 6, over: true, exact: false, looked: 0 }, [],
    ])
  })

  it('past the cap after one lookup: a folder holding a non-match opens its children', async () => {
    // Round 1: `b/s` full (1 item); `b/r` not: `tomat1`, `tomat2` settle (2), `b/r/u` opens (1) → 4 > 3.
    const look = lookupOf(RUNS)
    expect([await coverFloor(matchRoots(RUNS, 'tomat', ''), '', look, { minDepth: 2, cap: 3 }), look.calls]).toEqual([
      { n: 4, over: true, exact: false, looked: 2 }, [['b/r', 'b/s']],
    ])
  })

  it('more roots than the cap, but a full folder collapses them: under the cap, exact (the cover decides)', async () => {
    const roots = matchRoots(ALL, 'tomat', '')
    const look = lookupOf(ALL)
    const floor = await coverFloor(roots, '', look, { minDepth: 2, cap: 3 })
    const cover = await coverSet(roots, '', lookupOf(ALL), { minDepth: 2 })
    expect([roots.length, floor, look.calls, cover.items.map(i => [i.path, i.roots])]).toEqual([
      5, { n: 1, over: false, exact: true, looked: 1 }, [['b/all']], [['b/all', 5]],
    ])
  })

  it('a lookup over budget stops at the bound so far: not over, not exact', async () => {
    const look = lookupOf(RUNS, 0)
    expect([await coverFloor(matchRoots(RUNS, 'tomat', ''), '', look, { minDepth: 2, cap: 3 }), look.calls]).toEqual([
      { n: 2, over: false, exact: false, looked: 0 }, [['b/r', 'b/s']],
    ])
  })

  it('the FIX tree: exact once nothing is open, equal to the cover\'s size', async () => {
    const roots = matchRoots(FIX, 'tomat', '')
    const cover = await coverSet(roots, '', lookupOf(FIX), { minDepth: 2 })
    expect([await coverFloor(roots, '', lookupOf(FIX), { minDepth: 2, cap: 6 }), cover.items.length]).toEqual([
      { n: 6, over: false, exact: true, looked: 4 }, 6,
    ])
  })

  it('a drilled view: the view is the floor folder (never above it)', async () => {
    const look = lookupOf(FIX)
    // Under `b/deep` (depth 2 = the floor): one open folder, `b/deep` itself, full → one item.
    expect([await coverFloor(matchRoots(FIX, 'tomat', 'b/deep'), 'b/deep', look, { minDepth: 2, cap: 0 }), look.calls]).toEqual([
      { n: 1, over: true, exact: false, looked: 0 }, [],
    ])
    expect(await coverFloor(matchRoots(FIX, 'tomat', 'b/deep'), 'b/deep', lookupOf(FIX), { minDepth: 2, cap: 1 })).toEqual({ n: 1, over: false, exact: true, looked: 1 })
  })

  it('on random trees: never above the cover (any budget, any view), exact = the cover\'s size, over ⇒ the cover is over', async () => {
    // mulberry32: 32-bit integer math throughout (an LCG in doubles loses its low bits).
    let seed = 11
    const rnd = (n: number) => {
      seed = (seed + 0x6d2b79f5) | 0
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
      return ((t ^ (t >>> 14)) >>> 0) % n
    }
    const names = ['tomat', 'tomato_x', 'a', 'b', 'c', 'xtomatx', 'd']
    let exacts = 0, overs = 0, cut = 0
    for (let trial = 0; trial < 300; trial++) {
      const s: Store = {}
      for (let i = 0, n = 1 + rnd(16); i < n; i++) {
        const segs = ['bk' + rnd(2), ...Array.from({ length: 1 + rnd(4) }, () => names[rnd(names.length)])]
        const p = segs.join('/')
        if (Object.keys(s).some(o => o.startsWith(p + '/') || p.startsWith(o + '/'))) continue
        s[p] = rnd(3) === 0 ? 0 : 1 + rnd(50)
      }
      const view = rnd(3) === 0 ? 'bk0' : ''
      const roots = matchRoots(s, 'tomat', view)
      const items = (await coverSet(roots, view, lookupOf(s), { minDepth: 2 })).items.length
      const budget = rnd(2) ? Infinity : rnd(3)
      const cap = rnd(items + 2)
      const f = await coverFloor(roots, view, lookupOf(s, budget), { minDepth: 2, cap })
      expect([trial, f.n <= items, f.over ? items > cap : true, f.exact ? f.n === items : true]).toEqual([trial, true, true, true])
      if (f.exact) exacts++
      if (f.over) overs++
      if (!f.exact && !f.over) cut++
    }
    // Every branch is exercised.
    expect([exacts > 0, overs > 0, cut > 0]).toEqual([true, true, true])
  })

  it('mutation: a bound that never collapses (every folder "not full") is the root count, and refuses a cover of one', async () => {
    const roots = matchRoots(ALL, 'tomat', '')
    const never = await coverFloor(roots, '', lookupOf(ALL), { minDepth: 2, cap: 3, full: () => false })
    expect([never, (await coverSet(roots, '', lookupOf(ALL), { minDepth: 2 })).items.length]).toEqual([
      { n: 5, over: true, exact: true, looked: 1 }, 1,
    ])
  })
})
