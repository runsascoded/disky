import { describe, it, expect } from 'vitest'
import { unionOutline, groupRects, groupOutlines, type Rect } from '../src/outlines'
import type { PlacedCell } from '../src/layout'

const r = (x: number, y: number, w: number, h: number): Rect => ({ x, y, w, h })

describe('unionOutline', () => {
  it('a single rect → its four edges, each with an inward normal', () => {
    expect(unionOutline([r(0, 0, 10, 20)])).toEqual([
      { x1: 0, y1: 0, x2: 0, y2: 20, nx: 1, ny: 0 },
      { x1: 10, y1: 0, x2: 10, y2: 20, nx: -1, ny: 0 },
      { x1: 0, y1: 0, x2: 10, y2: 0, nx: 0, ny: 1 },
      { x1: 0, y1: 20, x2: 10, y2: 20, nx: 0, ny: -1 },
    ])
  })

  it('two horizontally-adjacent rects → the outer perimeter (shared edge cancels)', () => {
    expect(unionOutline([r(0, 0, 10, 10), r(10, 0, 10, 10)])).toEqual([
      { x1: 0, y1: 0, x2: 0, y2: 10, nx: 1, ny: 0 },
      { x1: 20, y1: 0, x2: 20, y2: 10, nx: -1, ny: 0 },
      { x1: 0, y1: 0, x2: 20, y2: 0, nx: 0, ny: 1 },
      { x1: 0, y1: 10, x2: 20, y2: 10, nx: 0, ny: -1 },
    ])
  })

  it('two disjoint rects → two separate perimeters (8 edges)', () => {
    const segs = unionOutline([r(0, 0, 10, 10), r(20, 0, 10, 10)])
    expect(segs.length).toBe(8)
    // The gap line x=10..20 contributes no segment; each rect keeps all four.
    expect(segs.filter(s => s.x1 === s.x2).map(s => s.x1).sort((a, b) => a - b)).toEqual([0, 10, 20, 30])
  })

  it('staggered rects (partial edge overlap) → the L-shaped union perimeter', () => {
    // A: small top-left; B: tall on the right. They share x=10 only over [0,10].
    expect(unionOutline([r(0, 0, 10, 10), r(10, 0, 10, 20)])).toEqual([
      { x1: 0, y1: 0, x2: 0, y2: 10, nx: 1, ny: 0 },
      { x1: 10, y1: 10, x2: 10, y2: 20, nx: 1, ny: 0 },
      { x1: 20, y1: 0, x2: 20, y2: 20, nx: -1, ny: 0 },
      { x1: 0, y1: 0, x2: 20, y2: 0, nx: 0, ny: 1 },
      { x1: 0, y1: 10, x2: 10, y2: 10, nx: 0, ny: -1 },
      { x1: 10, y1: 20, x2: 20, y2: 20, nx: 0, ny: -1 },
    ])
  })

  it('float-epsilon seams: neighbors whose shared seam differs by ε → the shared edge still cancels', () => {
    // Squarify hands the left rect a right edge and the right rect a left edge
    // that agree only to float epsilon. Without ⅛-px snapping they bucket onto
    // two distinct lines and the seam strokes twice; snapped, it cancels and the
    // union is the same clean perimeter as the exactly-adjacent case.
    const eps = 1e-9
    expect(unionOutline([r(0, 0, 10, 10), r(10 + eps, 0, 10 - eps, 10)])).toEqual([
      { x1: 0, y1: 0, x2: 0, y2: 10, nx: 1, ny: 0 },
      { x1: 20, y1: 0, x2: 20, y2: 10, nx: -1, ny: 0 },
      { x1: 0, y1: 0, x2: 20, y2: 0, nx: 0, ny: 1 },
      { x1: 0, y1: 10, x2: 20, y2: 10, nx: 0, ny: -1 },
    ])
  })

  it('float-epsilon seams: no zero-length segment survives at a snapped-collinear seam', () => {
    // A seam offset by less than ½ of ⅛ px snaps flush; the symmetric-difference
    // there is empty, so no `hi <= lo` segment (which square caps would paint as
    // an lw×lw dot) is emitted.
    const segs = unionOutline([r(0, 0, 10, 10), r(9.99, 0, 10, 10)])
    expect(segs.every(s => s.x1 !== s.x2 || s.y1 !== s.y2)).toBe(true)
    expect(segs.filter(s => s.x1 === s.x2).map(s => s.x1).sort((a, b) => a - b)).toEqual([0, 20])
  })
})

function pc(
  node: string,
  x: number,
  y: number,
  w: number,
  h: number,
  children: PlacedCell<string>[] = [],
): PlacedCell<string> {
  return { node, path: [node], folded: false, x, y, w, h, depth: 0, children } as unknown as PlacedCell<string>
}

describe('groupRects', () => {
  it('a same-key child nested under its parent is dropped (parent covers it)', () => {
    const cells = [pc('A', 0, 0, 20, 20, [pc('A', 2, 2, 16, 16)])]
    expect(groupRects(cells, n => n)).toEqual(new Map([['A', [r(0, 0, 20, 20)]]]))
  })

  it('sibling cells sharing a key are both kept (outermost each)', () => {
    const cells = [pc('R', 0, 0, 20, 10, [pc('A', 0, 0, 10, 10), pc('B', 10, 0, 10, 10)])]
    // Fate key: siblings A and B both carry mark 'm'; the root carries none.
    expect(groupRects(cells, n => (n === 'R' ? null : 'm'))).toEqual(
      new Map([['m', [r(0, 0, 10, 10), r(10, 0, 10, 10)]]]),
    )
  })
})

describe('groupOutlines', () => {
  it('adjacent same-mark siblings resolve to one region outline', () => {
    const cells = [pc('R', 0, 0, 20, 10, [pc('A', 0, 0, 10, 10), pc('B', 10, 0, 10, 10)])]
    expect(groupOutlines(cells, { key: n => (n === 'R' ? null : 'm'), color: () => '#0f0' })).toEqual([
      {
        key: 'm',
        color: '#0f0',
        segs: [
          { x1: 0, y1: 0, x2: 0, y2: 10, nx: 1, ny: 0 },
          { x1: 20, y1: 0, x2: 20, y2: 10, nx: -1, ny: 0 },
          { x1: 0, y1: 0, x2: 20, y2: 0, nx: 0, ny: 1 },
          { x1: 0, y1: 10, x2: 20, y2: 10, nx: 0, ny: -1 },
        ],
      },
    ])
  })
})
