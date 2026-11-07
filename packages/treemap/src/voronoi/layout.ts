/// <reference path="./d3-voronoi-treemap.d.ts" />
/**
 * Thin, deterministic wrapper over `d3-voronoi-treemap` (spec: viz-widgets.md §6).
 *
 * The solver is iterative: it nudges site weights until each cell's area
 * matches its value share, stopping at `convergenceRatio` or
 * `maxIterationCount`. Two consequences the callers must not paper over:
 *
 * 1. **It can stop short of converging.** We return the achieved error so the
 *    UI can label an unconverged layout instead of implying exact areas.
 * 2. **It is random-seeded.** Left alone it uses `Math.random`, so the same
 *    data lays out differently every render (cells visibly jump). We always
 *    seed it, making the layout a pure function of (values, clip, seed).
 */

import { hierarchy } from 'd3-hierarchy'
import { voronoiTreemap } from 'd3-voronoi-treemap'
import { maxAreaError, polygonArea, polygonCentroid } from './geometry'
import type { Point, Polygon } from './geometry'
import { hashSeed, mulberry32 } from './prng'

export interface VoronoiCell<T> {
  node: T
  value: number
  polygon: Polygon
  centroid: Point
  area: number
}

export interface VoronoiLayout<T> {
  cells: VoronoiCell<T>[]
  /** Items dropped for being too small to tessellate (see `minShare`). */
  excluded: number
  /** Their combined value — so a caller can say what fraction is missing. */
  excludedValue: number
  /**
   * Worst *per-cell relative* area error — `|actual − target| / target`, the
   * error a reader of the chart would perceive ("this cell is 5% too big").
   */
  error: number
  /** Whether `error` came in under `tolerance`. Label the chart when false. */
  converged: boolean
}

export interface VoronoiLayoutOpts {
  /**
   * The solver's own stop criterion, in units of **clip area**: it stops once
   * cell-area error falls under this fraction of the whole clipping polygon.
   * That is not a per-cell relative bound — at d3's default (0.01) a cell
   * holding 20% of the area can still land ~5% off its target. We default to
   * `0.001`, which measured ~0.01% relative error on a 3-cell square versus
   * ~1.3% at 0.01, for a handful of extra iterations.
   */
  convergenceRatio?: number
  /** Hard iteration cap. Default 200. */
  maxIterationCount?: number
  /**
   * Drop items below this fraction of the total before laying out. Default
   * 0.005.
   *
   * This is not tidiness, it's a hard limit of the technique: the solver
   * cannot shrink a cell arbitrarily (see `minWeightRatio`), so on a real
   * directory listing — where the biggest child outweighs the smallest by
   * six orders of magnitude — micro-cells come out *thousands of percent*
   * too large and drag the whole tessellation off target. Measured on a
   * 13-child listing: 421,710% worst-case error unfiltered, 0.14% at 0.005.
   * Rect treemaps have no such floor; this is one reason they stay the
   * default view.
   */
  minShare?: number
  /**
   * Smallest site weight the solver will keep, as a fraction of the largest.
   * Anything under it is clamped *up*, so a cell far below the floor renders
   * much bigger than its value — the library's 0.01 default makes a child
   * holding 0.1% of the bytes come out ~200% too large. Default 1e-4.
   */
  minWeightRatio?: number
  /** Layout seed — same seed + data ⇒ same picture. Default: 1. */
  seed?: number | string
  /**
   * Max acceptable per-cell relative area error before `converged` goes
   * false (and the component labels the chart as approximate). Default 0.02.
   */
  tolerance?: number
}

/**
 * Tessellate `clip` so each item's cell area is proportional to its value.
 * Non-positive values are dropped (a zero-area cell has no meaning here).
 */
export function voronoiLayout<T>(
  items: T[],
  getValue: (n: T) => number,
  clip: Polygon,
  opts: VoronoiLayoutOpts = {},
): VoronoiLayout<T> {
  const positive = items.filter(n => getValue(n) > 0)
  const total = positive.reduce((s, n) => s + getValue(n), 0)
  const minShare = opts.minShare ?? 0.005
  const kept = positive.filter(n => getValue(n) / total >= minShare)
  const excludedItems = positive.length - kept.length
  const excludedValue = total - kept.reduce((s, n) => s + getValue(n), 0)
  if (kept.length === 0 || clip.length < 3) {
    return { cells: [], error: 0, converged: true, excluded: excludedItems, excludedValue }
  }
  const convergenceRatio = opts.convergenceRatio ?? 0.001
  const maxIterationCount = opts.maxIterationCount ?? 200
  const tolerance = opts.tolerance ?? 0.02
  const seed = typeof opts.seed === 'string' ? hashSeed(opts.seed) : opts.seed ?? 1

  const root = hierarchy({ children: kept.map(node => ({ node })) } as never)
    .sum((d: never) => {
      const leaf = d as unknown as { node?: T }
      return leaf.node ? getValue(leaf.node) : 0
    })

  const vt = voronoiTreemap()
    .clip(clip)
    .minWeightRatio(opts.minWeightRatio ?? 1e-4)
    .convergenceRatio(convergenceRatio)
    .maxIterationCount(maxIterationCount)
    .prng(mulberry32(seed))
  vt(root)

  const cells: VoronoiCell<T>[] = []
  for (const leaf of root.children ?? []) {
    const polygon = (leaf as unknown as { polygon?: Polygon }).polygon
    const node = (leaf.data as unknown as { node: T }).node
    if (!polygon || polygon.length < 3) continue
    cells.push({
      node,
      value: getValue(node),
      polygon,
      centroid: polygonCentroid(polygon),
      area: polygonArea(polygon),
    })
  }
  const error = maxAreaError(cells)
  return { cells, error, converged: error <= tolerance, excluded: excludedItems, excludedValue }
}
