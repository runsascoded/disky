/**
 * Clip polygons and area math for Voronoi treemaps (spec: viz-widgets.md §6).
 *
 * Kept separate from the component so the geometry — the part with actual
 * invariants (area ∝ value, cells tile the clip) — is unit-testable without a
 * DOM.
 */

export type Point = [number, number]
export type Polygon = Point[]

/** Regular `sides`-gon approximating a circle — the clip for glyph mode. */
export function circlePolygon(cx: number, cy: number, r: number, sides = 64): Polygon {
  return Array.from({ length: sides }, (_, i) => {
    const t = (2 * Math.PI * i) / sides
    return [cx + r * Math.cos(t), cy + r * Math.sin(t)] as Point
  })
}

/** Axis-aligned rectangle as a clip polygon. */
export function rectPolygon(x: number, y: number, w: number, h: number): Polygon {
  return [
    [x, y],
    [x + w, y],
    [x + w, y + h],
    [x, y + h],
  ]
}

/** Shoelace area, always positive (winding-order independent). */
export function polygonArea(poly: Polygon): number {
  let a = 0
  for (let i = 0, n = poly.length; i < n; i++) {
    const [x0, y0] = poly[i]
    const [x1, y1] = poly[(i + 1) % n]
    a += x0 * y1 - x1 * y0
  }
  return Math.abs(a) / 2
}

/** Area-weighted centroid — where a cell's label wants to sit. */
export function polygonCentroid(poly: Polygon): Point {
  let a = 0
  let cx = 0
  let cy = 0
  for (let i = 0, n = poly.length; i < n; i++) {
    const [x0, y0] = poly[i]
    const [x1, y1] = poly[(i + 1) % n]
    const cross = x0 * y1 - x1 * y0
    a += cross
    cx += (x0 + x1) * cross
    cy += (y0 + y1) * cross
  }
  if (a === 0) {
    // Degenerate (zero-area) cell: fall back to the vertex mean.
    const n = poly.length || 1
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n]
  }
  return [cx / (3 * a), cy / (3 * a)]
}

/** SVG `points` attribute for a polygon. */
export function toPointsAttr(poly: Polygon, digits = 2): string {
  return poly.map(([x, y]) => `${x.toFixed(digits)},${y.toFixed(digits)}`).join(' ')
}

/**
 * Largest relative area error across cells: `max |actual/total − value/Σvalue|
 * / (value/Σvalue)`. The Voronoi solver is iterative, so this is how you check
 * a layout actually converged rather than assuming it did.
 */
export function maxAreaError(cells: { polygon: Polygon; value: number }[]): number {
  const totalValue = cells.reduce((s, c) => s + c.value, 0)
  const totalArea = cells.reduce((s, c) => s + polygonArea(c.polygon), 0)
  if (!(totalValue > 0) || !(totalArea > 0)) return 0
  let worst = 0
  for (const c of cells) {
    const target = c.value / totalValue
    const actual = polygonArea(c.polygon) / totalArea
    if (target > 0) worst = Math.max(worst, Math.abs(actual - target) / target)
  }
  return worst
}
