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
