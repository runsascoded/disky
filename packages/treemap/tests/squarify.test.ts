import { describe, expect, it } from 'vitest'
import { foldSmall, foldThin, squarify, squarifyRemainder } from '../src/squarify'
import type { Rect } from '../src/squarify'

/** Sort rects by `it` for deterministic comparison. */
const byIt = <T>(rects: Rect<T>[], key: (t: T) => string) =>
  [...rects].sort((a, b) => key(a.it).localeCompare(key(b.it)))

interface N {
  n: string
  b: number
}

const sz = (n: N) => n.b

describe('squarify', () => {
  it('drops zero-weight items', () => {
    const rects = squarify<N>(
      [{ n: 'a', b: 0 }, { n: 'b', b: 100 }],
      0, 0, 100, 100, sz,
    )
    expect(rects.map(r => r.it.n)).toEqual(['b'])
  })

  it('empty input returns empty rects', () => {
    expect(squarify<N>([], 0, 0, 100, 100, sz)).toEqual([])
  })

  it('zero-area rect returns empty rects', () => {
    expect(squarify<N>([{ n: 'a', b: 100 }], 0, 0, 0, 100, sz)).toEqual([])
    expect(squarify<N>([{ n: 'a', b: 100 }], 0, 0, 100, 0, sz)).toEqual([])
  })

  it('single item fills the rect exactly', () => {
    const rects = squarify<N>([{ n: 'a', b: 100 }], 0, 0, 100, 100, sz)
    expect(rects).toEqual([{ it: { n: 'a', b: 100 }, x: 0, y: 0, w: 100, h: 100 }])
  })

  it('areas are proportional to weights (sum == container area)', () => {
    const items: N[] = [
      { n: 'a', b: 60 },
      { n: 'b', b: 30 },
      { n: 'c', b: 10 },
    ]
    const rects = squarify(items, 0, 0, 200, 100, sz)
    const totalArea = rects.reduce((s, r) => s + r.w * r.h, 0)
    // Container is 20000; areas sum to that within float tolerance.
    expect(totalArea).toBeCloseTo(20_000, 2)
    // Per-item area is proportional to weight.
    const sorted = byIt(rects, it => it.n)
    const areas = new Map(sorted.map(r => [r.it.n, r.w * r.h]))
    expect(areas.get('a')! / 60).toBeCloseTo(areas.get('b')! / 30, 3)
    expect(areas.get('a')! / 60).toBeCloseTo(areas.get('c')! / 10, 3)
  })

  it('rects tile the container without overlap', () => {
    const items: N[] = Array.from({ length: 6 }, (_, i) => ({ n: `x${i}`, b: 10 + i }))
    const rects = squarify(items, 0, 0, 300, 200, sz)
    // Every rect stays inside the container.
    for (const r of rects) {
      expect(r.x).toBeGreaterThanOrEqual(-1e-6)
      expect(r.y).toBeGreaterThanOrEqual(-1e-6)
      expect(r.x + r.w).toBeLessThanOrEqual(300 + 1e-6)
      expect(r.y + r.h).toBeLessThanOrEqual(200 + 1e-6)
    }
    // Sum of areas equals container area.
    const total = rects.reduce((s, r) => s + r.w * r.h, 0)
    expect(total).toBeCloseTo(300 * 200, 2)
  })
})

describe('foldSmall', () => {
  const mergeSmall = (small: N[]): N => ({
    n: `(+${small.length})`,
    b: small.reduce((s, it) => s + it.b, 0),
  })

  it('leaves items alone when everything is big enough', () => {
    const items: N[] = [{ n: 'a', b: 50 }, { n: 'b', b: 50 }]
    // Container area is 10_000; each item is scaled to 5000 px² — well above minArea=16
    const out = foldSmall(items, 100, 100, sz, mergeSmall, 16)
    expect(out).toEqual(items)
  })

  it('folds multiple sub-threshold items into one synthetic node', () => {
    // Container area 100 px²; 4 items of weight 1 each ⇒ 25 px² each.
    // 1 item of weight 96 ⇒ 96 px². Set minArea=30 so only the big one survives.
    const items: N[] = [
      { n: 'big', b: 96 },
      { n: 't1', b: 1 },
      { n: 't2', b: 1 },
      { n: 't3', b: 1 },
      { n: 't4', b: 1 },
    ]
    const out = foldSmall(items, 10, 10, sz, mergeSmall, 30)
    expect(out.map(o => o.n)).toEqual(['big', '(+4)'])
    expect(out.find(o => o.n === '(+4)')!.b).toBe(4)
  })

  it('leaves items alone when only one is sub-threshold (nothing to merge)', () => {
    const items: N[] = [
      { n: 'big1', b: 45 },
      { n: 'big2', b: 45 },
      { n: 'tiny', b: 1 },
    ]
    // Only one tiny item — merger would produce a synthetic "(+1)" that
    // isn't smaller than the item it replaces, so foldSmall skips it.
    const out = foldSmall(items, 10, 10, sz, mergeSmall, 5)
    expect(out).toEqual(items)
  })
})

describe('foldThin', () => {
  const mergeSmall = (small: N[]): N => ({
    n: `(+${small.length})`,
    b: small.reduce((s, it) => s + it.b, 0),
  })

  it('merges cells whose short side is below the threshold', () => {
    // Three 2px-wide slivers + one wide cell.
    const rects: Rect<N>[] = [
      { it: { n: 'wide', b: 1 }, x: 0, y: 0, w: 90, h: 100 },
      { it: { n: 's1', b: 1 }, x: 90, y: 0, w: 2, h: 33 },
      { it: { n: 's2', b: 1 }, x: 90, y: 33, w: 2, h: 33 },
      { it: { n: 's3', b: 1 }, x: 90, y: 66, w: 2, h: 34 },
    ]
    const out = foldThin(rects, 7, mergeSmall)
    expect(out).not.toBeNull()
    expect(out!.map(o => o.n)).toEqual(['wide', '(+3)'])
    expect(out!.find(o => o.n === '(+3)')!.b).toBe(3)
  })

  it('returns null when fewer than two cells are thin (nothing to fold)', () => {
    const rects: Rect<N>[] = [
      { it: { n: 'a', b: 1 }, x: 0, y: 0, w: 50, h: 100 },
      { it: { n: 'thin', b: 1 }, x: 50, y: 0, w: 3, h: 100 },
    ]
    expect(foldThin(rects, 7, mergeSmall)).toBeNull()
  })

  it('a dominant sibling produces slivers that foldThin then collapses', () => {
    // 99%-dominant item squeezes three tiny siblings into a ~4px strip.
    const items: N[] = [
      { n: 'big', b: 990 },
      { n: 't1', b: 4 },
      { n: 't2', b: 3 },
      { n: 't3', b: 3 },
    ]
    const laid = squarify<N>(items, 0, 0, 400, 300, sz)
    const slivers = laid.filter(r => Math.min(r.w, r.h) < 7)
    expect(slivers.length).toBeGreaterThanOrEqual(2) // the defect: unhoverable slivers
    const refolded = foldThin(laid, 7, mergeSmall)!
    const relaid = squarify<N>(refolded, 0, 0, 400, 300, sz)
    // The many slivers collapse to at most one thin cell (the merged tile).
    expect(relaid.filter(r => Math.min(r.w, r.h) < 7).length).toBeLessThanOrEqual(1)
    expect(relaid.map(r => r.it.n).sort()).toEqual(['(+3)', 'big'])
  })
})

describe('squarifyRemainder', () => {
  it('gives a long tail a wider 2D band instead of slivers (area preserved)', () => {
    // One dominant item + a cramped tail that plain squarify squeezes thin.
    const items: N[] = [
      { n: 'big', b: 9800 },
      { n: 'a', b: 60 }, { n: 'b', b: 50 }, { n: 'c', b: 40 },
      { n: 'd', b: 30 }, { n: 'e', b: 20 },
    ]
    const w = 800, h = 300
    const plain = squarify<N>(items, 0, 0, w, h, sz)
    const rem = squarifyRemainder<N>(items, 0, 0, w, h, sz, 28, 0.18)
    const shortSide = (rects: Rect<N>[], n: string) => {
      const r = rects.find(x => x.it.n === n)!
      return Math.min(r.w, r.h)
    }
    // A tail cell renders wider under the remainder layout.
    expect(shortSide(rem, 'a')).toBeGreaterThan(shortSide(plain, 'a'))
    // It actually triggered (not a plain fallthrough).
    expect(rem).not.toEqual(plain)
    // Still tiles the whole container.
    expect(rem.reduce((s, r) => s + r.w * r.h, 0)).toBeCloseTo(w * h, -1)
    for (const r of rem) {
      expect(r.x).toBeGreaterThanOrEqual(-1e-6)
      expect(r.x + r.w).toBeLessThanOrEqual(w + 1e-6)
    }
  })

  it('falls back to plain squarify when no tail is cramped', () => {
    const items: N[] = [{ n: 'a', b: 50 }, { n: 'b', b: 50 }]
    expect(squarifyRemainder<N>(items, 0, 0, 200, 200, sz, 7, 0.14))
      .toEqual(squarify<N>(items, 0, 0, 200, 200, sz))
  })
})
