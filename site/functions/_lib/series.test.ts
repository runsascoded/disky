import { describe, expect, it } from 'vitest'
import { changesNote, metaRoots, rootPoints, stepFlows, withFlows, wirePoint } from './series'
import type { ChangeEntry, Flows, RootTrace, SeriesPoint } from './series'
import { sumPoints } from './groups'
import { carryForward } from '../../src/federatedViews'

const P = 'marin-us-east-02a'
const H = 'hero-checkpoints'

describe('metaRoots', () => {
  it('a multi-bucket meta names its roots', () => {
    expect(metaRoots({ total_bytes: 30, total_objects: 3, buckets: { [P]: { total_bytes: 20, total_objects: 2 }, [H]: { total_bytes: 10, total_objects: 1 } } }, P))
      .toEqual([{ path: P, b: 20, o: 2 }, { path: H, b: 10, o: 1 }])
  })
  it('a flat meta is the sole root’s, and nothing without a sole root', () => {
    expect(metaRoots({ total_bytes: 20, total_objects: 2 }, P)).toEqual([{ path: P, b: 20, o: 2 }])
    expect(metaRoots({ total_bytes: 20 }, P)).toEqual([{ path: P, b: 20, o: 0 }])
    expect(metaRoots({ total_bytes: 20, total_objects: 2 }, null)).toEqual([])
    expect(metaRoots({}, P)).toEqual([])
  })
})

describe('rootPoints', () => {
  it('one trace per root, ordered by first appearance then latest bytes; a late root’s trace starts at its genesis', () => {
    const byDate = new Map([
      ['2026-09-16T1200', [{ path: P, b: 100, o: 10 }]],
      ['2026-09-15T0000', [{ path: P, b: 90, o: 9 }]],
      ['2026-09-17T0000', [{ path: H, b: 40, o: 4 }, { path: P, b: 101, o: 11 }]],
    ])
    expect(rootPoints(byDate)).toEqual<RootTrace[]>([
      { path: P, points: [{ date: '2026-09-15T0000', b: 90, o: 9 }, { date: '2026-09-16T1200', b: 100, o: 10 }, { date: '2026-09-17T0000', b: 101, o: 11 }] },
      { path: H, points: [{ date: '2026-09-17T0000', b: 40, o: 4 }] },
    ])
  })
  it('same genesis: larger root first', () => {
    const byDate = new Map([['2026-09-17T0000', [{ path: 'a', b: 1, o: 1 }, { path: 'b', b: 5, o: 1 }]]])
    expect(rootPoints(byDate).map(t => t.path)).toEqual(['b', 'a'])
  })
  it('no scans: no traces', () => {
    expect(rootPoints(new Map())).toEqual([])
  })
})

// ── Change flows (specs/change-metrics.md §4.1) ──

const F = (added: number, deleted: number, modified: number, ab = 0, db = 0, mb = 0): Flows =>
  ({ added, deleted, modified, added_bytes: ab, deleted_bytes: db, modified_bytes: mb })
const by = (token: number, size: number, mtime: number) => ({ token, size, mtime })
const entry = (since: string, paths: Record<string, [Flows, ReturnType<typeof by>]>): ChangeEntry =>
  ({ since, mtime: 'write', paths: Object.fromEntries(Object.entries(paths).map(([p, [f, m]]) => [p, { ...f, mods_by: m }])) })

describe('withFlows', () => {
  const pts = [{ date: 'd1', b: 10, o: 3 }, { date: 'd2', b: 12, o: 4 }, { date: 'd4', b: 11, o: 4 }]
  const changes = new Map([
    ['d2', entry('d1', { '': [F(2, 1, 1, 20, 5, 7), by(1, 0, 0)], a: [F(2, 1, 0, 20, 5, 0), by(0, 0, 0)] })],
    // d3 isn't in the series: d4's changes are since it
    ['d4', entry('d3', { '': [F(0, 0, 3, 0, 0, 9), by(0, 2, 1)] })],
  ])
  it("a counted scan's flows at the path (zero where nothing changed under it), `since` where it isn't the point before", () => {
    expect(withFlows(pts, '', changes)).toEqual([
      { date: 'd1', b: 10, o: 3 },
      { date: 'd2', b: 12, o: 4, ch: F(2, 1, 1, 20, 5, 7) },
      { date: 'd4', b: 11, o: 4, ch: F(0, 0, 3, 0, 0, 9), since: 'd3' },
    ])
    expect(withFlows(pts, 'a', changes).map(p => p.ch ?? null)).toEqual([null, F(2, 1, 0, 20, 5, 0), F(0, 0, 0)])
    expect(withFlows(pts, '', changes).map(wirePoint)).toEqual([
      { date: 'd1', bytes: 10, objects: 3 },
      { date: 'd2', bytes: 12, objects: 4, added: 2, deleted: 1, modified: 1, added_bytes: 20, deleted_bytes: 5, modified_bytes: 7 },
      { date: 'd4', bytes: 11, objects: 4, added: 0, deleted: 0, modified: 3, added_bytes: 0, deleted_bytes: 0, modified_bytes: 9, since: 'd3' },
    ])
  })
  it("the window's first counted scan and its mods' basis, summed", () => {
    expect([changesNote(pts, '', changes), changesNote(pts, 'a', changes), changesNote(pts.slice(0, 1), '', changes)]).toEqual([
      { from: 'd2', mods_by: by(1, 2, 1) }, { from: 'd2', mods_by: by(0, 0, 0) }, null,
    ])
  })
})

describe('summed flows', () => {
  const p = (date: string, bytes: number, objects: number, f?: Flows): SeriesPoint => ({ date, bytes, objects, ...(f ?? {}) })
  it("a group's point sums its members' flows, and has none where a member's point lacks them", () => {
    expect(sumPoints([
      [p('d1', 1, 1), p('d2', 2, 2, F(1, 0, 0, 5)), p('d3', 3, 3, F(0, 1, 0, 0, 4))],
      [p('d2', 10, 10, F(3, 1, 2, 30, 10, 20)), p('d3', 10, 10)],
    ])).toEqual([
      p('d1', 1, 1),
      p('d2', 12, 12, F(4, 1, 2, 35, 10, 20)),
      p('d3', 13, 13),
    ])
  })
  it("a union step sums the flows of the members that scanned at it, never a carried point's", () => {
    // member A scans at t1 and t3, member B at t2: B's flows count at t2 only, though its point carries to t3
    const a1 = p('a1', 10, 10, F(1, 0, 0)), a3 = p('a3', 11, 11, F(2, 1, 0)), b2 = p('b2', 5, 5, F(0, 0, 4))
    const traces = [
      { source: 'A', points: [{ at: 't1', value: a1 }, { at: 't3', value: a3 }] },
      { source: 'B', points: [{ at: 't2', value: b2 }] },
    ]
    expect(carryForward(traces).map(s => [s.at, stepFlows(s.at, s.values, traces)])).toEqual([
      ['t1', F(1, 0, 0)], ['t2', F(0, 0, 4)], ['t3', F(2, 1, 0)],
    ])
    // a member scanning at the step without flows: none for the step
    const bare = [traces[0], { source: 'B', points: [{ at: 't1', value: p('b1', 5, 5) }] }]
    expect(carryForward(bare).map(s => stepFlows(s.at, s.values, bare))).toEqual([null, F(2, 1, 0)])
  })
})
