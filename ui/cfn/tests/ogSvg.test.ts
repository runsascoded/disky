/** The edge-rendered link card (`cfn/ogSvg.ts`, tier B): byte/count formatting,
 *  the squarified cell layout (same `squarify`/`slotColor` as the live widget),
 *  and the SVG document it assembles. */
import { describe, expect, it } from 'vitest'
import { slotColor } from '@rdub/treemap'
import { cardCells, fmtBytes, fmtCount, treemapCardSvg } from '../ogSvg'

describe('fmtBytes', () => {
  it('formats base-1024 with one decimal under 100', () => {
    expect([0, 4, 1024, 5.3 * 1024 ** 3, 916.1 * 1024 ** 3, 200 * 1024 ** 2].map(fmtBytes)).toEqual([
      '0 B',
      '4 B',
      '1.0 K',
      '5.3 G',
      '916.1 G',
      '200.0 M',
    ])
  })
})

describe('fmtCount', () => {
  it('groups thousands', () => {
    expect([0, 336, 920859].map(fmtCount)).toEqual(['0', '336', '920,859'])
  })
})

describe('cardCells', () => {
  const children = [
    { name: 'gbfs', size: 300 },
    { name: '.dvc', size: 100 },
    { name: 'zero', size: 0 },
  ]
  const cells = cardCells(children, 96, 1200, 630)

  it('drops zero-size children, sorts by size desc, colors by slot', () => {
    expect(cells.map(c => [c.name, c.size, c.color])).toEqual([
      ['gbfs', 300, slotColor(0)],
      ['.dvc', 100, slotColor(1)],
    ])
  })

  it('tiles the treemap area exactly (Σ area == container area)', () => {
    const area = cells.reduce((s, c) => s + c.w * c.h, 0)
    expect(Math.abs(area - 1200 * (630 - 96))).toBeLessThan(1e-6)
  })

  it('colors match the package `slotColor` across the golden-angle range', () => {
    // cfn can't import the barrel; ogSvg mirrors `slotColor`. Pin the mirror to
    // the real thing (importable here — tests run with DOM) over 16 slots.
    const many = Array.from({ length: 16 }, (_, i) => ({ name: `d${i}`, size: 16 - i }))
    const colors = cardCells(many, 96, 1200, 630).map(c => c.color)
    expect(colors).toEqual(many.map((_, i) => slotColor(i)))
  })

  it('keeps every cell within the treemap area', () => {
    for (const c of cells) {
      expect(c.x).toBeGreaterThanOrEqual(0)
      expect(c.y).toBeGreaterThanOrEqual(96)
      expect(c.x + c.w).toBeLessThanOrEqual(1200 + 1e-6)
      expect(c.y + c.h).toBeLessThanOrEqual(630 + 1e-6)
    }
  })
})

describe('treemapCardSvg', () => {
  const svg = treemapCardSvg({
    uri: 'r2://ctbk',
    children: [{ name: 'gbfs', size: 300 }, { name: '.dvc', size: 100 }],
    itemCount: 336,
  })

  it('opens with a 1200x630 svg document', () => {
    expect(svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">')).toBe(true)
    expect(svg.endsWith('</svg>')).toBe(true)
  })

  it('draws one background rect plus one per positive-size child', () => {
    expect((svg.match(/<rect /g) ?? []).length).toBe(1 + 2)
  })

  it('renders the uri and header stats as text', () => {
    expect(svg).toContain('>r2://ctbk</text>')
    expect(svg).toContain('>400 B  ·  336 items</text>')
  })
})
