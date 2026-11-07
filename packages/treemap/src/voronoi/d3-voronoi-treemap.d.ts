/**
 * `d3-voronoi-treemap` ships no types and `@types/d3-voronoi-treemap` doesn't
 * exist; this declares the slice of its API `layout.ts` uses.
 */
declare module 'd3-voronoi-treemap' {
  type Polygon = [number, number][]

  interface VoronoiTreemap {
    (root: unknown): void
    clip(polygon: Polygon): VoronoiTreemap
    convergenceRatio(ratio: number): VoronoiTreemap
    maxIterationCount(count: number): VoronoiTreemap
    minWeightRatio(ratio: number): VoronoiTreemap
    prng(random: () => number): VoronoiTreemap
  }

  export function voronoiTreemap(): VoronoiTreemap
}
