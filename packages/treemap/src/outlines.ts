/**
 * Grouped-outline geometry: stroke the outer perimeter of the *union* of
 * rendered cells sharing a group key, once — so a run of adjacent same-mark
 * siblings reads as one bordered region, not a lattice of doubled cell frames
 * (spec `specs/treemap-mark-union-outlines.md`, asked for by mgu).
 *
 * The map's cells are axis-aligned, non-overlapping rects on one plane (a
 * tiling), so the union boundary is pure edge arithmetic — no polygon library.
 * A vertical line at `x` borders the group on its left where a group rect ends
 * at `x` and on its right where one begins at `x`; the boundary is exactly
 * where those two occupancies *disagree* (a rect on one side, none on the
 * other). Symmetric-difference of the two interval sets per line gives the
 * boundary segments and, from which side won, the inward normal (for insetting).
 */
import { type PlacedCell, isFolded } from './layout'

export interface OutlineGroups<T> {
  /** Group key for a cell, or `null` for none. Same key ⇒ same region. */
  key: (node: T, path: T[]) => string | null
  /** Stroke color for a group. */
  color: (key: string) => string
  /** Stroke width in CSS px (default 2). */
  width?: number
  /** Stroke just inside the union (default) or centered on its boundary. */
  inset?: boolean
  /**
   * Called after each paint with the keys that produced at least one segment
   * — what is actually on screen, so a consumer's key/legend can list only
   * the groups a viewer can see (a group whose cells all fold below the
   * pixel budget has no outline to explain). Keep it cheap and idempotent:
   * it runs on every relayout.
   */
  onDrawn?: (keys: string[]) => void
}

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

/** One boundary segment: endpoints plus the unit inward normal (toward the
 *  group's interior), which insetting offsets along. */
export interface OutlineSeg {
  x1: number
  y1: number
  x2: number
  y2: number
  nx: number
  ny: number
}

type Iv = [number, number]

/** Merge a set of closed intervals into sorted, disjoint intervals. */
function mergeIntervals(ivs: Iv[]): Iv[] {
  if (ivs.length === 0) return []
  const sorted = [...ivs].sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const out: Iv[] = [[...sorted[0]] as Iv]
  for (let i = 1; i < sorted.length; i++) {
    const cur = sorted[i]
    const last = out[out.length - 1]
    if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1])
    else out.push([...cur] as Iv)
  }
  return out
}

/** Intervals covered by exactly one of `a`/`b` (both already merged/disjoint),
 *  each tagged with which set owns it. Coordinate-compressed sweep over every
 *  endpoint; adjacent same-side pieces are coalesced. */
function symDiff(a: Iv[], b: Iv[]): { lo: number; hi: number; side: 'a' | 'b' }[] {
  const pts = new Set<number>()
  for (const [lo, hi] of a) { pts.add(lo); pts.add(hi) }
  for (const [lo, hi] of b) { pts.add(lo); pts.add(hi) }
  const xs = [...pts].sort((p, q) => p - q)
  const inSet = (ivs: Iv[], mid: number) => ivs.some(([lo, hi]) => lo < mid && mid < hi)
  const out: { lo: number; hi: number; side: 'a' | 'b' }[] = []
  for (let i = 0; i + 1 < xs.length; i++) {
    const lo = xs[i]
    const hi = xs[i + 1]
    if (hi <= lo) continue
    const mid = (lo + hi) / 2
    const inA = inSet(a, mid)
    const inB = inSet(b, mid)
    if (inA === inB) continue
    const side = inA ? 'a' : 'b'
    const last = out[out.length - 1]
    if (last && last.side === side && last.hi === lo) last.hi = hi
    else out.push({ lo, hi, side })
  }
  return out
}

/** Snap a coordinate to ⅛ px. Squarify hands neighbors seams that agree only
 *  to float epsilon; without snapping, the two rects bucket onto *different*
 *  lines and the shared seam strokes twice (a doubled bar, inset each way). */
const q = (v: number): number => Math.round(v * 8) / 8

/**
 * The boundary of the union of `rects` (assumed non-overlapping) as a set of
 * segments with inward normals. Vertical lines first, then horizontal.
 */
export function unionOutline(rects: Rect[]): OutlineSeg[] {
  const segs: OutlineSeg[] = []

  // Vertical boundaries: at each x, `left` = rects ending there (interior to
  // the left, normal −x), `right` = rects starting there (interior to the
  // right, normal +x). Boundary = where exactly one side is occupied.
  const byX = new Map<number, { left: Iv[]; right: Iv[] }>()
  const atX = (x: number) => {
    let e = byX.get(x)
    if (!e) { e = { left: [], right: [] }; byX.set(x, e) }
    return e
  }
  for (const r of rects) {
    const iv: Iv = [q(r.y), q(r.y + r.h)]
    atX(q(r.x)).right.push(iv)
    atX(q(r.x + r.w)).left.push(iv)
  }
  for (const [x, { left, right }] of byX) {
    for (const { lo, hi, side } of symDiff(mergeIntervals(left), mergeIntervals(right))) {
      if (hi <= lo) continue
      segs.push({ x1: x, y1: lo, x2: x, y2: hi, nx: side === 'a' ? -1 : 1, ny: 0 })
    }
  }

  // Horizontal boundaries: at each y, `above` = rects ending there (interior
  // above, normal −y), `below` = rects starting there (interior below, +y).
  const byY = new Map<number, { above: Iv[]; below: Iv[] }>()
  const atY = (y: number) => {
    let e = byY.get(y)
    if (!e) { e = { above: [], below: [] }; byY.set(y, e) }
    return e
  }
  for (const r of rects) {
    const iv: Iv = [q(r.x), q(r.x + r.w)]
    atY(q(r.y)).below.push(iv)
    atY(q(r.y + r.h)).above.push(iv)
  }
  for (const [y, { above, below }] of byY) {
    for (const { lo, hi, side } of symDiff(mergeIntervals(above), mergeIntervals(below))) {
      if (hi <= lo) continue
      segs.push({ x1: lo, y1: y, x2: hi, y2: y, nx: 0, ny: side === 'a' ? -1 : 1 })
    }
  }

  return segs
}

/**
 * Bucket placed cells into groups by key, keeping only the *outermost* cell for
 * each key on any path — a descendant with the same key as an ancestor is
 * already covered by the ancestor's rect (nesting), so including it would
 * overlap and break the union arithmetic. Folded/dust tiles whose node isn't a
 * real `T` are skipped. Insertion order follows first appearance (top-down).
 */
export function groupRects<T>(
  cells: PlacedCell<T>[],
  key: (node: T, path: T[]) => string | null,
): Map<string, Rect[]> {
  const groups = new Map<string, Rect[]>()
  const walk = (cell: PlacedCell<T>, openKeys: ReadonlySet<string>) => {
    const k = isFolded(cell.node) ? null : key(cell.node, cell.path)
    let open = openKeys
    if (k !== null && !openKeys.has(k)) {
      let rects = groups.get(k)
      if (!rects) { rects = []; groups.set(k, rects) }
      rects.push({ x: cell.x, y: cell.y, w: cell.w, h: cell.h })
      open = new Set(openKeys).add(k)
    }
    for (const c of cell.children) walk(c, open)
  }
  const empty: ReadonlySet<string> = new Set()
  for (const c of cells) walk(c, empty)
  return groups
}

export interface OutlinePath {
  key: string
  color: string
  segs: OutlineSeg[]
}

/** The stroked outlines for every group present in `cells`. */
export function groupOutlines<T>(
  cells: PlacedCell<T>[],
  opts: OutlineGroups<T>,
): OutlinePath[] {
  const out: OutlinePath[] = []
  for (const [key, rects] of groupRects(cells, opts.key)) {
    out.push({ key, color: opts.color(key), segs: unionOutline(rects) })
  }
  return out
}
