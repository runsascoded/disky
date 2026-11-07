import { describe, expect, it } from 'vitest'
import { metaRoots, rootPoints } from './series'
import type { RootTrace } from './series'

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
