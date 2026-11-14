import { describe, expect, it } from 'vitest'
import { flattenPlaced, layoutCells, type LayoutConfig, type PlacedCell } from '../src/layout'
import { squarify } from '../src/squarify'
import { resolveVars, staticLayout, treemapSvg } from '../src/svg'

interface N {
  n: string
  size: number
  children?: N[]
}

const getSize = (n: N) => n.size
const getLabel = (n: N) => n.n
const getChildren = (n: N) => n.children

const tree: N = {
  n: 'root',
  size: 300,
  children: [
    { n: 'foo', size: 200, children: [{ n: 'a.txt', size: 120 }, { n: 'b.txt', size: 80 }] },
    { n: 'bar', size: 100 },
  ],
}

const W = 400
const H = 300
const geom = (cells: PlacedCell<N>[]) => flattenPlaced(cells).map(c => [(c.node as N).n, c.x, c.y, c.w, c.h, c.depth, c.mode])

/** Texts as `[x, y, size, content]`, in paint order. */
const texts = (svg: string) => [...svg.matchAll(/<text x="([\d.]+)" y="([\d.]+)" font-size="([\d.]+)"[^>]*>([^<]*)<\/text>/g)].map(m => [Number(m[1]), Number(m[2]), Number(m[3]), m[4]])

describe('staticLayout', () => {
  it('lays out as layoutCells does with the component defaults', () => {
    const cfg: LayoutConfig<N> = {
      getSize, getLabel, childrenOf: getChildren, showLabels: true, collapseChains: false,
      borderWidth: d => Math.max(1, 2 - d), edgeEmphasis: 0,
      fold: raw => raw,
      layTiles: (items, x, y, w, h) => squarify(items, x, y, w, h, it => (it as N).size),
      tilingFor: () => 'shared',
    }
    const expected = layoutCells(squarify(tree.children!, 0, 0, W, H, getSize), [tree], 'shared', cfg)
    expect(geom(staticLayout(tree, { width: W, height: H, getSize, getLabel, getChildren, tiling: 'shared' }))).toEqual(geom(expected))
  })
})

describe('treemapSvg', () => {
  const cells = staticLayout(tree, { width: W, height: H, getSize, getLabel, getChildren, tiling: 'shared' })
  const svg = treemapSvg(cells, {
    width: W, height: H, getSize, getLabel, formatSize: n => `${n} B`,
    measure: (s, px) => s.length * px * 0.5,
  })
  it('labels each cell: names, inline sizes on branches, second-line sizes on tall leaves', () => {
    expect(texts(svg)).toEqual(TEXTS)
  })
  it('paints the opaque base and a faded fill per cell, plus the shared-edge strokes', () => {
    expect([...svg.matchAll(/<rect /g)].length).toBe(RECTS)
  })
})

/** The dust elements (a folded tile's dashed frame, the hatch's rules), in paint order. */
const dustEls = (svg: string) => [...svg.matchAll(/<rect [^>]*stroke-dasharray[^>]*\/>|<path [^>]*\/>/g)].map(m => m[0])

describe('treemapSvg dust', () => {
  const paintOpts = { getSize, getLabel, formatSize: (n: number) => `${n}`, measure: (s: string, px: number) => s.length * px * 0.5 }
  it("frames and cross-hatches a folded tail, in the light ink its faint ground on the dark base wants", () => {
    // 40 children of 15px² each fold into one 10×60 "(+40)" tile beside `big`.
    const kids = Array.from({ length: 40 }, (_, i) => ({ n: `t${i}`, size: 0.25 }))
    const root: N = { n: 'root', size: 100, children: [{ n: 'big', size: 90 }, ...kids] }
    const cells = staticLayout(root, { width: 100, height: 60, getSize, getLabel, getChildren, tiling: 'shared', showLabels: false })
    expect(dustEls(treemapSvg(cells, { width: 100, height: 60, ...paintOpts }))).toEqual([
      '<rect x="91" y="1" width="8" height="58" fill="none" stroke="rgb(255,255,255)" stroke-opacity="0.6" stroke-dasharray="2 2"/>',
      '<path d="M96 0.5V59.5M90.5 60H99.5M90.5 59H99.5M90.5 58H99.5M90.5 57H99.5M90.5 56H99.5M90.5 55H99.5M90.5 53H99.5M90.5 50H99.5M90.5 46H99.5M90.5 41H99.5M90.5 35H99.5M90.5 27H99.5M90.5 16H99.5M90.5 1H99.5" fill="none" stroke="rgb(255,255,255)" stroke-opacity="0.6" stroke-width="1"/>',
    ])
  })
  it("steps a labeled tile's rules around its label text, so none strikes through it", () => {
    // 40 children of 150px² each, under a 200px² floor, fold into one 60×100 "(+40)" tile.
    const kids = Array.from({ length: 40 }, (_, i) => ({ n: `t${i}`, size: 0.5 }))
    const root: N = { n: 'root', size: 100, children: [{ n: 'big', size: 80 }, ...kids] }
    const cells = staticLayout(root, { width: 300, height: 100, getSize, getLabel, getChildren, tiling: 'shared', minCellArea: 200 })
    const svg = treemapSvg(cells, { width: 300, height: 100, ...paintOpts })
    expect(texts(svg)).toEqual([[4, 12.8, 13.5, 'big'], [4, 27.7, 11.5, '80'], [244, 11.2, 11.5, '(+40)'], [244, 25.7, 11.5, '20']])
    // The label's text spans x 244–272.75 (5 chars × 5.75px) and y 0–28 (2 + 11.5 + 3 + 11.5); +2px, from the paint
    // box's corner (241, 1), that's a 33.75 × 29 notch: verticals left of x=274.75 start at y=30, horizontals above
    // y=30 start at x=275, and the rest run edge to edge (the frame keeps the whole box).
    expect(dustEls(svg)).toEqual([
      '<rect x="241.5" y="1.5" width="57" height="97" fill="none" stroke="rgb(255,255,255)" stroke-opacity="0.6" stroke-dasharray="2 2"/>',
      '<path d="M299.5 1V99M298.5 1V99M297.5 1V99M297.5 1V99M295.5 1V99M294.5 1V99M292.5 1V99M289.5 1V99M285.5 1V99M281.5 1V99M274.5 30V99M266.5 30V99M255.5 30V99M241 99.5H299M241 98.5H299M241 97.5H299M241 97.5H299M241 95.5H299M241 94.5H299M241 92.5H299M241 89.5H299M241 86.5H299M241 81.5H299M241 75.5H299M241 68.5H299M241 57.5H299M241 43.5H299M275 25.5H299" fill="none" stroke="rgb(255,255,255)" stroke-opacity="0.6" stroke-width="1"/>',
    ])
  })
  it("hatches a consumer aggregate (`dustCount`) with no frame, dimmed only by the cell's own opacity", () => {
    const root: N = { n: 'root', size: 100, children: [{ n: 'agg', size: 100 }] }
    const cells = staticLayout(root, { width: 40, height: 20, getSize, getLabel, getChildren, tiling: 'shared', showLabels: false })
    const svg = treemapSvg(cells, { width: 40, height: 20, ...paintOpts, colorForCell: () => ({ bg: 'rgb(55, 175, 75)', ink: '#fff', dustCount: 7, opacity: 0.5 }) })
    // Green at 0.46 (0.92 fade × 0.5) over the dark base is dark: white rules, at 0.6 × the 0.5 opacity.
    expect(dustEls(svg)).toEqual([
      '<path d="M38.5 1V19M36.5 1V19M35.5 1V19M32.5 1V19M29.5 1V19M24.5 1V19M19.5 1V19M11.5 1V19M1.5 1V19M1 16.5H39M1 12.5H39M1 8.5H39" fill="none" stroke="rgb(255,255,255)" stroke-opacity="0.3" stroke-width="1"/>',
    ])
    expect(dustEls(treemapSvg(cells, { width: 40, height: 20, ...paintOpts, dustTexture: false, colorForCell: () => ({ bg: 'rgb(55, 175, 75)', dustCount: 7 }) }))).toEqual([])
  })
})

describe('resolveVars', () => {
  it('substitutes known vars, falls back, and recurses', () => {
    expect(resolveVars('var(--a)', { '--a': '#123' })).toBe('#123')
    expect(resolveVars('var(--b, #456)', {})).toBe('#456')
    expect(resolveVars('var(--c, var(--a))', { '--a': 'rgb(1, 2, 3)' })).toBe('rgb(1, 2, 3)')
  })
})

// foo's bar holds its name and, 6px on, its size; its kids sit inside the 1px
// half-edge + 4px pad; tall leaves take their size on a second, smaller line.
const TEXTS = [
  [4, 12.8, 13.5, 'foo'],
  [30.25, 12.8, 13.5, '200 B'],
  [5, 32.8, 13.5, 'a.txt'],
  [5, 47.7, 11.5, '120 B'],
  [270.67, 12.8, 13.5, 'bar'],
  [270.67, 27.7, 11.5, '100 B'],
  [5, 200.2, 13.5, 'b.txt'],
  [5, 215.1, 11.5, '80 B'],
]
// The map ground, then per cell an opaque base + faded fill, and a contrast
// edge on the 3 leaves (the container's fill is the ground's: no edge)
const RECTS = 1 + 4 * 2 + 3
