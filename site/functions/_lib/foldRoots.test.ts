// A match-roots answer's forest as a view holds it: the nodes it can use, whatever the number of roots (`foldRoots`).
import { describe, expect, it } from 'vitest'
import { HELD_BYTES } from './shared.js'
import { dirOrder, foldPhase1, type FoldOpts, foldRoots, MATCH_LIST_CAP, matchLists, nestRollup, tooBroadToDiff } from './view.js'
import type { Hit } from './staticFilter.js'

const A = '2026-10-01T0700', B = '2026-10-02T0700', C = '2026-10-03T0700'
const ms = (d: string) => Date.UTC(Number(d.slice(0, 4)), Number(d.slice(5, 7)) - 1, Number(d.slice(8, 10)), 7)
const FOREVER = ms('2099-01-01T0700')
const depthOf = (p: string) => p.split('/').length
const hit = (path: string, size: number, vf = A, vt?: string, usr = ''): Hit => ({ path, depth: depthOf(path), usr, vf: ms(vf), vt: vt ? ms(vt) : FOREVER, size: BigInt(size), n: 1n })
const isRoot = (p: string) => p.split('/').pop()!.includes('ckpt')
const thrAt = (T: number) => (d: number) => T * 2 ** Math.max(0, d - 2)
const fold = (hits: Hit[], o: { date?: string; other?: string; T?: number; last?: number } = {}) =>
  foldRoots('b', hits, { date: o.date ?? B, other: o.other, thrAt: thrAt(o.T ?? 100), last: o.last ?? Infinity, isRoot })

describe('foldRoots', () => {
  it('preserves live owner slices for retained nodes and the complete answered directory without holding discarded nodes', () => {
    const f = fold([
      hit('b/x/ckpt-1', 400, A, B, 'old'),
      hit('b/x/ckpt-1', 300, B, undefined, 'alice'),
      hit('b/x/ckpt-1', 200, B, undefined, 'bob'),
      hit('b/tiny/ckpt-2', 1, B, undefined, 'alice'),
    ])
    expect(f).toEqual({
      nodes: [
        { p: 'b/x/ckpt-1', d: 3, isDir: false, b: 500, n: 2, ub: { alice: 300, bob: 200 } },
        { p: 'b/x', d: 2, isDir: true, b: 500, n: 2, ub: { alice: 300, bob: 200 } },
        { p: 'b/tiny/ckpt-2', d: 3, isDir: false, b: 1, n: 1, ub: { alice: 1 } },
      ],
      kids: new Map([['b/x', 1], ['b', 2]]), owners: { alice: 301, bob: 200 },
    })
  })
  it('keeps the drawable nodes and the heaviest other roots, sums every other node away, and counts the kids of drawable nodes', () => {
    const hits = [
      hit('b/x/y/ckpt-1', 500),
      hit('b/x/y/ckpt-2', 10),
      // `b/w` (10 bytes) is under the threshold: never drawn. Its roots stay as the match list's overflow (there is room).
      hit('b/w/v/ckpt-3', 6),
      hit('b/w/v/ckpt-4', 4),
    ]
    expect(fold(hits)).toEqual({
      nodes: [
        { p: 'b/x/y/ckpt-1', d: 4, isDir: false, b: 500, n: 1 },
        { p: 'b/x/y', d: 3, isDir: true, b: 510, n: 2 },
        { p: 'b/x', d: 2, isDir: true, b: 510, n: 2 },
        { p: 'b/x/y/ckpt-2', d: 4, isDir: false, b: 10, n: 1 },
        { p: 'b/w/v/ckpt-3', d: 4, isDir: false, b: 6, n: 1 },
        { p: 'b/w/v/ckpt-4', d: 4, isDir: false, b: 4, n: 1 },
      ],
      kids: new Map([['b/x/y', 2], ['b/x', 1], ['b', 2]]),
    })
  })

  it('holds a bounded number of nodes, whatever the number of roots', () => {
    const light = Array.from({ length: 5000 }, (_, i) => hit(`b/d${i % 50}/e${i}/ckpt-${i}`, 1))
    const { nodes, kids } = fold([hit('b/big/ckpt', 1000), ...light], { T: 200 })
    const roots = light.map(h => h.path).sort()
    // Equal bytes: first by path, as the match list ranks them.
    expect(nodes.map(n => n.p).sort()).toEqual(['b/big', 'b/big/ckpt', ...roots.slice(0, MATCH_LIST_CAP)].sort())
    expect(kids).toEqual(new Map([['b/big', 1], ['b', 51]]))
  })

  it("keeps this side's row for what the other scan draws, not its children", () => {
    // Grown between the scans: drawable on `to` only, under the threshold on `from`.
    const hits = [hit('b/x/y/ckpt-1', 5, A, B), hit('b/x/y/ckpt-1', 1000, B), hit('b/x/z', 7)]
    const row = (date: string, other?: string) => { const f = fold(hits, { date, other }); return [f.nodes.map(n => [n.p, n.b]), [...f.kids]] }
    expect(row(A, B)).toEqual([[['b/x/y/ckpt-1', 5], ['b/x/y', 5], ['b/x', 12]], [['b', 1]]])
    expect(row(B, A)).toEqual([[['b/x/y/ckpt-1', 1000], ['b/x/y', 1000], ['b/x', 1007]], [['b/x/y', 1], ['b/x', 2], ['b', 1]]])
    // A lone read has no other scan's: only its own list's overflow of that forest (`b/x/z` has no `ckpt`).
    expect(row(A)).toEqual([[['b/x/y/ckpt-1', 5]], [['b', 1]]])
    // Live on the other scan alone: no row on this side.
    expect(fold([hit('b/x/ckpt-9', 1000, B)], { date: A, other: B })).toEqual({ nodes: [], kids: new Map() })
  })

  it("ranks the match list's overflow on the newer scan's bytes, else the older's, the same on both sides", () => {
    const small = Array.from({ length: MATCH_LIST_CAP - 1 }, (_, i) => hit(`b/s${i}/t/ckpt-${String(i).padStart(3, '0')}`, 5))
    // Gone on the newer scan: ranks on its older 50. Live on both, 1 byte on the newer: the lightest, cut, though 90 on the older.
    const hits = [...small, hit('b/old/t/ckpt-old', 50, A, B), hit('b/new/t/ckpt-new', 90, A, B), hit('b/new/t/ckpt-new', 1, B)]
    const rootsOn = (date: string, other: string) => fold(hits, { date, other, T: 1000 }).nodes.map(n => n.p)
    expect(rootsOn(A, B).sort()).toEqual([...small.map(h => h.path), 'b/old/t/ckpt-old'].sort())
    expect(rootsOn(B, A).sort()).toEqual(small.map(h => h.path).sort())
  })

  it('reads only the scan asked for', () => {
    const hits = [hit('b/x/ckpt', 200, A, B), hit('b/x/ckpt', 300, C)]
    expect(fold(hits, { date: B })).toEqual({ nodes: [], kids: new Map() })
    expect(fold(hits, { date: C })).toEqual({
      nodes: [{ p: 'b/x/ckpt', d: 3, isDir: false, b: 300, n: 1 }, { p: 'b/x', d: 2, isDir: true, b: 300, n: 1 }],
      kids: new Map([['b/x', 1], ['b', 1]]),
    })
  })

  it('treats a node past `last` as undrawable', () => {
    expect(fold([hit('b/x/y/ckpt', 1000)], { last: 2 })).toEqual({
      nodes: [{ p: 'b/x/y/ckpt', d: 4, isDir: false, b: 1000, n: 1 }, { p: 'b/x', d: 2, isDir: true, b: 1000, n: 1 }],
      kids: new Map([['b/x', 1], ['b', 1]]),
    })
  })
})

describe('tooBroadToDiff', () => {
  it('refuses a diff of an answer past what an isolate holds of answers', () => {
    const roots = (n: number) => Array.from({ length: n }, () => ({ path: 'p'.repeat(100) }))
    const fits = Math.floor(HELD_BYTES / 356)
    expect([tooBroadToDiff(roots(fits)), tooBroadToDiff(roots(fits + 1))]).toEqual([false, true])
  })
})

describe('nestRollup over a roots answer', () => {
  it('propagates a fetched roots answer’s owners into the rollup ancestors without changing their exact totals', async () => {
    const result = (await nestRollup({
      date: B, path: '', dP: 0, rollup: { dir: '', kept: 1, rows: 2, children: 1, cells: [{ kind: 1, child: 'b', vf: ms(A), b: 501n, o: 3n }] },
      isRoot, threshold: 100, w: 1792, h: 1088, minArea: 12, atten: 2, ms: 10_000, force: new Set(['b']),
      fetch: async () => ({ hits: [hit('b/x/ckpt-1', 300, B, undefined, 'alice'), hit('b/x/ckpt-1', 200, B, undefined, 'bob'), hit('b/tiny/ckpt-2', 1, B, undefined, 'alice')], io: {} }),
    }))!
    expect([result.total.ub, result.exact.get('b')!.ub, result.exact.get('b/x')!.ub, result.exact.get('b/x/ckpt-1')!.ub]).toEqual([
      { alice: 301, bob: 200 }, { alice: 301, bob: 200 }, { alice: 300, bob: 200 }, { alice: 300, bob: 200 },
    ])
    expect([result.total.b, result.total.o, result.exact.bo('b')]).toEqual([501, 3, { b: 501, o: 3 }])
  })
  const rollup = { dir: '', kept: 1, rows: 1, children: 1, cells: [{ kind: 1 as const, child: 'b', vf: ms(A), b: 6000n, o: 5001n }] }
  const light = Array.from({ length: 5000 }, (_, i) => hit(`b/d${i % 50}/e${i}/ckpt-${i}`, 1))
  const hits = [hit('b/big/ckpt', 1000), ...light]
  const nest = () => nestRollup({
    date: B, path: '', dP: 0, rollup, isRoot, threshold: 400, w: 1792, h: 1088, minArea: 12, atten: 2, ms: 60_000, force: new Set(['b']),
    fetch: async () => ({ hits, io: {} }),
  })

  it("holds the fold's nodes, not the forest's, and every drawn node's whole fold count", async () => {
    const n = (await nest())!
    // `b`, `b/big`, and the match list's overflow, whose heaviest is `b/big/ckpt` (its tile is under its floor).
    expect(n.exact.size).toBe(1 + 1 + MATCH_LIST_CAP)
    expect(n.kidsOf.get('b')).toEqual(['b/big'])
    expect(n.kidsOf.get('b/big')).toEqual(['b/big/ckpt'])
    expect(n.rollupKids).toEqual(new Map([['', 1], ['b/big', 1], ['b', 51]]))
    expect(n.exact.bo('b/big')).toEqual({ b: 1000, o: 1 })
    expect(n.exact.bo('b/big/ckpt')).toEqual({ b: 1000, o: 1 })
  })
})

describe('foldPhase1', () => {
  const opts = (o: Partial<FoldOpts> = {}): FoldOpts => ({ date: A, other: B, to: B, T: 100, atten: 2, budget: 2, subdivMin: 1e12, ...o })
  const hits = [
    hit('b/x/y/ckpt-1', 500),
    hit('b/x/y/ckpt-2', 10),
    hit('b/w/v/ckpt-3', 6),
    hit('b/w/v/ckpt-4', 4),
    // Live on `B` alone: drawn there, so a root this side counts but doesn't hold.
    hit('b/z/ckpt-5', 300, B),
  ]
  const shape = (m: ReturnType<typeof foldPhase1>) => ({
    roots: m.roots,
    mine: [...m.p1.mine].map(([p, a]) => [p, a.b, a.o]),
    nb: [...m.forest!.nb],
    sums: [...m.forest!.sums].map(([p, a]) => [p, a.b, a.o]),
    // Read by key only (`kids.get`): its order is the walk's, not part of the answer.
    kids: [...m.forest!.kids!].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
    matched: m.forest!.matched.map(x => [x.path, x.b]),
    total: [m.forest!.total.b, m.forest!.total.o],
    nRoots: m.forest!.nRoots,
    union: m.forest!.union,
  })

  it("holds each scan's `budget` heaviest drawable nodes and its drawn roots, counts the rest into their parents, and lists the others", () => {
    expect(shape(foldPhase1('b', 1, hits, opts()))).toEqual({
      // Over their floors on `A` (100, 200, 400 at depths 2–4): `b/x`, `b/x/y` (the budget's 2) and the drawn root `ckpt-1`.
      roots: ['b/x/y/ckpt-1'],
      mine: [['b/x/y/ckpt-1', 500, 1]],
      nb: [['b/x/y/ckpt-1', 500], ['b/x/y', 510], ['b/x', 510]],
      sums: [['b/x/y', 510, 2], ['b/x', 510, 2]],
      kids: [['b', 2], ['b/x', 1], ['b/x/y', 2]],
      matched: [['b/x/y/ckpt-1', 500], ['b/x/y/ckpt-2', 10], ['b/w/v/ckpt-3', 6], ['b/w/v/ckpt-4', 4]],
      total: [520, 4],
      nRoots: 4,
      union: { n: 5, b: 820, o: 5 },
    })
    // The other side: `ckpt-5` is its own; `b/x`'s chain the same, being the heaviest on both.
    expect(shape(foldPhase1('b', 1, hits, opts({ date: B, other: A }))).roots).toEqual(['b/x/y/ckpt-1', 'b/z/ckpt-5'])
  })

  it('holds a bounded number of nodes, whatever the number of roots', () => {
    const light = Array.from({ length: 5000 }, (_, i) => hit(`b/d${i % 50}/e${i}/ckpt-${i}`, 1))
    // Each `b/dN` holds 100 bytes, under its floor (200).
    const s = shape(foldPhase1('b', 1, [hit('b/big/ckpt', 100_000), ...light], opts({ budget: 10, T: 200 })))
    expect([s.roots, s.nb.length, s.sums.length, s.matched.length, s.nRoots, s.union]).toEqual([
      ['b/big/ckpt'], 2, 1, 1 + MATCH_LIST_CAP, 5001, { n: 5001, b: 105_000, o: 5001 },
    ])
    expect(s.kids).toEqual([['b', 51], ['b/big', 1]])
  })

  it('`dirOrder`: a path just before its descendants, which follow it in one run (plain order puts `b/x.y` among them)', () => {
    const ps = ['b/x/z', 'b/x.y', 'b/x', 'b/w', 'b/x/a.b', 'b/x/a', 'b/x/a/c', 'b', 'b/x/a!']
    expect([...ps].sort(dirOrder)).toEqual(['b', 'b/w', 'b/x.y', 'b/x', 'b/x/a!', 'b/x/a.b', 'b/x/a', 'b/x/a/c', 'b/x/z'])
    expect([dirOrder('b/x', 'b/x'), dirOrder('b/x', 'b/x/a'), dirOrder('b/x/a', 'b/x'), dirOrder('b/x.y', 'b/x'), dirOrder('b/x', 'b/x.y')]).toEqual([0, -1, 1, -1, 1])
  })

  it('a root under another root, and a sibling whose name goes on past it with a char below `/`: one node each, sums exact', () => {
    // `b/x` is a root and the parent of `b/x/z/ckpt`'s chain; `b/x.y/ckpt` sorts between them as plain strings. Sizes
    // near 2^51: their sums (below 2^53) are exact.
    const big = 2 ** 51
    const hs = [hit('b/x/z/ckpt', big + 5), hit('b/x', big + 100), hit('b/x.y/ckpt', 7), hit('b/x/z/ckpt', 3, A, B)]
    const m = foldPhase1('b', 1, hs, opts({ date: A, other: A, to: A, T: 1, budget: 100 }))
    expect([shape(m), m.forest!.nodes]).toEqual([{
      roots: ['b/x', 'b/x.y/ckpt', 'b/x/z/ckpt'],
      mine: [['b/x', 2 * big + 108, 3], ['b/x.y/ckpt', 7, 1], ['b/x/z/ckpt', big + 8, 2]],
      nb: [['b/x', 2 * big + 108], ['b/x.y/ckpt', 7], ['b/x/z/ckpt', big + 8], ['b/x.y', 7], ['b/x/z', big + 8]],
      sums: [['b/x.y', 7, 1], ['b/x/z', big + 8, 2], ['b/x', 2 * big + 108, 3]],
      kids: [['b', 2], ['b/x', 1], ['b/x.y', 1], ['b/x/z', 1]],
      matched: [['b/x', 2 * big + 108], ['b/x/z/ckpt', big + 8], ['b/x.y/ckpt', 7]],
      total: [2 * big + 115, 4],
      nRoots: 3,
      union: { n: 3, b: 3 * big + 123, o: 6 },
    }, 5])
  })

  it("a view's (no other scan: `other` = `to` = `date`) holds the same bound, and lists what the whole list would", () => {
    const light = Array.from({ length: 5000 }, (_, i) => hit(`b/d${i % 50}/e${i}/ckpt-${i}`, 1 + (i % 7)))
    const hits = [hit('b/big/ckpt', 100_000), ...light]
    const m = foldPhase1('b', 1, hits, opts({ date: A, other: A, to: A, budget: 10, T: 1000 }))
    const s = shape(m)
    const lightB = light.reduce((n, h) => n + Number(h.size), 0)
    expect([s.roots, s.nb, s.sums, s.matched.length, s.nRoots, s.union]).toEqual([
      ['b/big/ckpt'], [['b/big/ckpt', 100_000], ['b/big', 100_000]], [['b/big', 100_000, 1]], 1 + MATCH_LIST_CAP, 5001, { n: 5001, b: 100_000 + lightB, o: 5001 },
    ])
    // The response's lists from the held ones and the count are the lists from every root.
    const every = hits.map(h => ({ path: h.path, b: Number(h.size), o: 1 })).sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : 1))
    const kept = new Map([['b/big', 1], ['b/big/ckpt', 1]])
    expect(matchLists(m.forest!.matched, kept, m.forest!.union)).toEqual(matchLists(every, kept))
  })
})
