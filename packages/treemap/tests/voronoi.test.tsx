import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { VoronoiTreemap } from '../src/voronoi/VoronoiTreemap'
import {
  circlePolygon,
  maxAreaError,
  polygonArea,
  polygonCentroid,
  rectPolygon,
  toPointsAttr,
} from '../src/voronoi/geometry'
import { voronoiLayout } from '../src/voronoi/layout'
import { hashSeed, mulberry32 } from '../src/voronoi/prng'

const SQUARE = rectPolygon(0, 0, 100, 100)

interface Item {
  name: string
  size: number
}

const ITEMS: Item[] = [
  { name: 'a', size: 50 },
  { name: 'b', size: 30 },
  { name: 'c', size: 20 },
]

describe('geometry', () => {
  it('computes polygon area by the shoelace formula, winding-independent', () => {
    expect(polygonArea(SQUARE)).toBe(10_000)
    expect(polygonArea([...SQUARE].reverse())).toBe(10_000)
    expect(polygonArea([[0, 0], [4, 0], [4, 3]])).toBe(6)
  })

  it('computes the area centroid', () => {
    expect(polygonCentroid(SQUARE)).toEqual([50, 50])
    expect(polygonCentroid(rectPolygon(10, 20, 4, 8))).toEqual([12, 24])
  })

  it('falls back to the vertex mean for a degenerate polygon', () => {
    expect(polygonCentroid([[0, 0], [10, 0], [20, 0]])).toEqual([10, 0])
  })

  it('approximates a circle closely enough for a clip', () => {
    const exact = Math.PI * 100
    const area = polygonArea(circlePolygon(0, 0, 10, 64))
    // A 64-gon inscribes ~99.9% of its circle — invisible at chart scale.
    expect(Math.abs(area - exact) / exact).toBeLessThan(0.005)
    expect(circlePolygon(0, 0, 10, 64).length).toBe(64)
  })

  it('formats SVG points at 2 decimals', () => {
    expect(toPointsAttr([[1.234, 5.678], [9, 10]])).toBe('1.23,5.68 9.00,10.00')
  })

  it('maxAreaError is 0 when areas match values exactly', () => {
    expect(maxAreaError([
      { polygon: rectPolygon(0, 0, 50, 100), value: 1 },
      { polygon: rectPolygon(50, 0, 50, 100), value: 1 },
    ])).toBe(0)
  })

  it('maxAreaError reports the worst relative miss', () => {
    // Areas 60/40 against target 50/50 → 20% off on both.
    const err = maxAreaError([
      { polygon: rectPolygon(0, 0, 60, 100), value: 1 },
      { polygon: rectPolygon(60, 0, 40, 100), value: 1 },
    ])
    expect(Number(err.toPrecision(6))).toBe(0.2)
  })
})

describe('mulberry32 / hashSeed', () => {
  it('is deterministic per seed and differs across seeds', () => {
    const a = mulberry32(42)
    const b = mulberry32(42)
    const c = mulberry32(43)
    const seqA = [a(), a(), a()]
    expect([b(), b(), b()]).toEqual(seqA)
    expect([c(), c(), c()]).not.toEqual(seqA)
  })

  it('stays in [0, 1)', () => {
    const r = mulberry32(7)
    const xs = Array.from({ length: 200 }, () => r())
    expect(xs.every(x => x >= 0 && x < 1)).toBe(true)
  })

  it('hashSeed is stable and path-sensitive', () => {
    expect(hashSeed('a/b')).toBe(hashSeed('a/b'))
    expect(hashSeed('a/b')).not.toBe(hashSeed('a/c'))
  })
})

describe('voronoiLayout', () => {
  it('gives every item a cell whose area matches its value share', () => {
    const { cells, converged, error } = voronoiLayout(ITEMS, i => i.size, SQUARE)
    expect(cells.map(c => c.node.name)).toEqual(['a', 'b', 'c'])
    expect(converged).toBe(true)
    // Tighter than the naked eye and than d3's clip-relative default: at
    // convergenceRatio 0.001 a 3-cell square lands within ~0.01% per cell.
    expect(error).toBeLessThan(0.001)
  })

  it('tiles the clip: cell areas sum to the clip area', () => {
    const { cells } = voronoiLayout(ITEMS, i => i.size, SQUARE)
    const total = cells.reduce((s, c) => s + c.area, 0)
    expect(Math.abs(total - polygonArea(SQUARE)) / polygonArea(SQUARE)).toBeLessThan(0.01)
  })

  it('is deterministic for a given seed, and differs across seeds', () => {
    const pts = (seed: number) =>
      voronoiLayout(ITEMS, i => i.size, SQUARE, { seed }).cells.map(c => toPointsAttr(c.polygon))
    expect(pts(1)).toEqual(pts(1))
    expect(pts(1)).not.toEqual(pts(99))
  })

  it('drops non-positive values instead of emitting zero-area cells', () => {
    const items = [...ITEMS, { name: 'empty', size: 0 }, { name: 'neg', size: -5 }]
    const { cells } = voronoiLayout(items, i => i.size, SQUARE)
    expect(cells.map(c => c.node.name)).toEqual(['a', 'b', 'c'])
  })

  it('returns nothing for an empty item list or a degenerate clip', () => {
    expect(voronoiLayout([], (i: Item) => i.size, SQUARE).cells).toEqual([])
    expect(voronoiLayout(ITEMS, i => i.size, [[0, 0], [1, 1]]).cells).toEqual([])
  })

  it('reports non-convergence rather than pretending the areas are exact', () => {
    const { converged, error } = voronoiLayout(ITEMS, i => i.size, SQUARE, {
      maxIterationCount: 1,
      convergenceRatio: 1e-9,
      tolerance: 1e-9,
    })
    expect(converged).toBe(false)
    expect(error).toBeGreaterThan(0)
  })
})

describe('<VoronoiTreemap>', () => {
  it('renders one polygon per item', () => {
    const { container } = render(<VoronoiTreemap items={ITEMS} getValue={i => i.size} />)
    expect(container.querySelectorAll('polygon').length).toBe(3)
  })

  it('labels only cells with room for a label', () => {
    const { container } = render(
      <VoronoiTreemap items={ITEMS} getValue={i => i.size} getLabel={i => i.name} shape="rect" />,
    )
    const labels = [...container.querySelectorAll('text')].map(t => t.textContent)
    expect(labels).toEqual(['a', 'b', 'c'])
  })

  it('calls onCellClick with the clicked item', () => {
    const onCellClick = vi.fn()
    const { container } = render(
      <VoronoiTreemap items={ITEMS} getValue={i => i.size} onCellClick={onCellClick} />,
    )
    fireEvent.click(container.querySelectorAll('.dt-voronoi-cell')[1])
    expect(onCellClick.mock.calls.map(([i]) => i.name)).toEqual(['b'])
  })

  it('shows a tooltip on hover', () => {
    const { container } = render(
      <VoronoiTreemap items={ITEMS} getValue={i => i.size} getLabel={i => i.name} />,
    )
    fireEvent.mouseEnter(container.querySelectorAll('.dt-voronoi-cell')[0])
    expect(container.querySelector('.dt-voronoi-tip')!.textContent).toBe('a')
  })

  it('warns in-place when the layout did not converge', () => {
    const { container } = render(
      <VoronoiTreemap
        items={ITEMS}
        getValue={i => i.size}
        maxIterationCount={1}
        convergenceRatio={1e-9}
        tolerance={1e-9}
      />,
    )
    expect(container.querySelector('.dt-voronoi-note')!.textContent).toMatch(
      /^areas approximate \(max \d+\.\d% off target\)$/,
    )
  })

  it('renders nothing for an empty item list', () => {
    const { container } = render(<VoronoiTreemap items={[]} getValue={(i: Item) => i.size} />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.querySelector('.dt-voronoi')).toBeInTheDocument()
  })
})

describe('voronoiLayout minShare', () => {
  // A real listing's biggest child can outweigh its smallest by ~1e6; the
  // solver clamps tiny weights, so those cells render orders of magnitude too
  // large and wreck the whole tessellation.
  const WIDE = [1.8e9, 7.5e8, 6e8, 5.4e8, 6e6, 3.4e6, 1.6e6, 1.4e6, 4.9e5, 1.5e5, 4096, 4096]
    .map((v, i) => ({ name: `n${i}`, size: v }))

  it('excludes sub-threshold items and reports them', () => {
    const { cells, excluded, excludedValue } = voronoiLayout(WIDE, i => i.size, SQUARE)
    expect(cells.length).toBe(4)
    expect(excluded).toBe(8)
    expect(excludedValue).toBe(WIDE.slice(4).reduce((s, i) => s + i.size, 0))
  })

  it('converges once the range is bounded — and does not without', () => {
    expect(voronoiLayout(WIDE, i => i.size, SQUARE).converged).toBe(true)
    expect(voronoiLayout(WIDE, i => i.size, SQUARE, { minShare: 0 }).converged).toBe(false)
  })

  it('counts non-positive values as neither kept nor excluded', () => {
    const { cells, excluded } = voronoiLayout(
      [...ITEMS, { name: 'zero', size: 0 }],
      i => i.size,
      SQUARE,
    )
    expect([cells.length, excluded]).toEqual([3, 0])
  })
})

describe('<VoronoiTreemap> exclusions', () => {
  const WIDE = [1e9, 5e8, 4e8, 1000].map((v, i) => ({ name: `n${i}`, size: v }))

  it('says what it left out, formatted by the consumer', () => {
    const { container } = render(
      <VoronoiTreemap
        items={WIDE}
        getValue={i => i.size}
        formatValue={v => `${v} B`}
      />,
    )
    expect(container.querySelector('.dt-voronoi-note')!.textContent).toBe('1 too small to tessellate (1000 B)')
  })

  it('stays quiet when everything fits', () => {
    const { container } = render(<VoronoiTreemap items={ITEMS} getValue={i => i.size} />)
    expect(container.querySelector('.dt-voronoi-note')).toBeNull()
  })
})
