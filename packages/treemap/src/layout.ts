/**
 * Renderer-agnostic treemap layout.
 *
 * `layoutCells` turns a tree into a *placed-cell tree* — geometry plus node
 * identity, no style, no paint. It exists so the DOM renderer (`<Treemap>`'s
 * `cell()`) and the canvas renderer (`<TreemapCanvas>`) share one layout, and
 * a hit-test can walk the same retained tree a paint pass drew from.
 *
 * The formulas here mirror `Treemap.cell()` exactly (box insets, the
 * `w>90 && h>44` recursion gate, the shared/gaps inner offsets, chain
 * collapse), so the two renderers place cells identically and the DOM path can
 * later adopt this as its single source of truth.
 */
import type { Rect } from './squarify'
import type { Tiling } from './Treemap'

/** Synthetic node the default fold-small merger returns. */
export interface FoldedNode<T> {
  __folded: true
  count: number
  size: number
  children: T[]
}

export function isFolded<T>(n: T | FoldedNode<T>): n is FoldedNode<T> {
  return typeof n === 'object' && n !== null && (n as FoldedNode<T>).__folded === true
}

/** A laid-out cell: absolute map coordinates + node identity + nesting. */
export interface PlacedCell<T> {
  /** The node this cell represents (a `FoldedNode` for the folded tail). */
  node: T | FoldedNode<T>
  /** Full ancestry path ending at `node` (folded tiles reuse the parent path). */
  path: T[]
  folded: boolean
  /** Collapsed single-child spine labels, when `collapseChains` merged one. */
  chainLabels: string[] | null
  x: number
  y: number
  w: number
  h: number
  depth: number
  mode: Tiling
  /** Shared-mode half-stroke width in px (0 in gaps mode). */
  edge: number
  /** Rendered small enough (< 14px short side) to be a "dust" cell. */
  dust: boolean
  /** Cell box after the gaps gutter / shared inset — the region children fill. */
  boxW: number
  boxH: number
  showLbl: boolean
  hasKids: boolean
  /** Width of the parent's title hover region; null keeps the full-width bar. */
  titleWidth: number | null
  children: PlacedCell<T>[]
}

type Node<T> = T | FoldedNode<T>

/**
 * Shared-edge width multiplier that emphasizes shallow (top-level) boundaries
 * over deep ones, so the tree's coarse structure reads at a glance. At
 * `emphasis = 0` it's 1 everywhere (no change); higher values thicken depth-0
 * and depth-1 edges, decaying to 1 by depth 2. Used identically by both
 * renderers so their strokes match.
 */
export function edgeEmphFactor(depth: number, emphasis: number): number {
  return 1 + emphasis * Math.max(0, 2 - depth)
}

/** Everything `layoutCells` needs from the host component, threaded as one bundle. */
export interface LayoutConfig<T> {
  getSize: (n: T) => number
  getLabel: (n: T) => string
  /** Resolve a node's children (already merged with any lazily-fetched set). */
  childrenOf: (n: T, path: T[]) => T[] | undefined
  showLabels: boolean
  collapseChains: boolean
  borderWidth: (depth: number, ctx: { w: number; h: number }) => number
  /** Shallow-edge emphasis (see {@link edgeEmphFactor}); 0 = uniform. */
  edgeEmphasis: number
  /** Measured title content width. Absent keeps a full-width title strip. */
  getTitleWidth?: (n: T, chainLabels: string[] | null, w: number, boxW: number) => number
  /** Fold small/thin items before layout (the component's `fold`). */
  fold: (raw: Node<T>[], w: number, h: number) => Node<T>[]
  /** Lay already-folded items into a box (plain squarify or `squarifyRemainder`). */
  layTiles: (items: Node<T>[], x: number, y: number, w: number, h: number) => Rect<Node<T>>[]
  /** Decide a node's children's tiling mode from their laid-out density. */
  tilingFor: (
    n: T,
    path: T[],
    depth: number,
    w: number,
    h: number,
    rs: { w: number; h: number }[],
  ) => Tiling
}

/** Reclaim a title strip while preserving every sibling's original area ratio. */
export function reclaimTitleColumns<T>(
  rects: Rect<T>[],
  titleRight: number,
  bodyHeight: number,
  lift: number,
): Rect<T>[] {
  if (bodyHeight <= 0 || lift <= 0 || ![titleRight, bodyHeight, lift].every(Number.isFinite)) return rects
  const epsilon = 1e-6
  let boundary: number | undefined
  let rightmost = -Infinity
  for (const r of [...rects].sort((a, b) => a.x - b.x)) {
    if (r.x >= titleRight && rightmost <= r.x + epsilon) {
      boundary = r.x
      break
    }
    rightmost = Math.max(rightmost, r.x + r.w)
  }
  if (boundary === undefined) return rects
  const width = rects.reduce((max, r) => Math.max(max, r.x + r.w), 0)
  const raisedHeight = bodyHeight + lift
  // One area multiplier for BOTH partitions. The raised partition grows
  // vertically and narrows horizontally; the unraised partition widens.
  // Their widths still sum to the parent width, and every tile gets the same
  // added area per byte. The wider left partition also protects the title.
  const areaScale = width * raisedHeight / (width * bodyHeight + boundary * lift)
  const rightScale = areaScale * bodyHeight / raisedHeight
  const newBoundary = boundary * areaScale
  const heightScale = raisedHeight / bodyHeight
  return rects.map(r => r.x >= boundary - epsilon
    ? { ...r, x: newBoundary + (r.x - boundary) * rightScale, w: r.w * rightScale, y: r.y * heightScale - lift, h: r.h * heightScale }
    : { ...r, x: r.x * areaScale, w: r.w * areaScale })
}

/** One nested layout for DOM, canvas, outlines and exports. Child coordinates are local to the inner box. */
export function layoutChildren<T>(
  node: T,
  path: T[],
  chainLabels: string[] | null,
  raw: T[] | undefined,
  dims: { w: number; h: number; boxW: number; boxH: number; edge: number; showLbl: boolean; depth: number },
  cfg: LayoutConfig<T>,
) {
  const { w, h, boxW, boxH, edge, showLbl, depth } = dims
  let kw = boxW - 4
  let kh = boxH - (showLbl ? 21 : 4)
  let mode: Tiling = 'gaps'
  let rects: Rect<Node<T>>[] = []
  let titleWidth: number | null = null
  if (raw?.length && w > 90 && h > 44) {
    const lay = (width: number, height: number) => cfg.layTiles(cfg.fold(raw.slice(), width, height), 0, 0, width, height)
    rects = lay(kw, kh)
    mode = cfg.tilingFor(node, path, depth + 1, w, h, rects)
    if (mode === 'shared') {
      kw = boxW - 2 * edge
      kh = boxH - (showLbl ? 20 + edge : 2 * edge)
      rects = lay(kw, kh)
    }
    if (showLbl && cfg.getTitleWidth) {
      const width = cfg.getTitleWidth(node, chainLabels, w, boxW)
      const left = mode === 'shared' ? edge : 3
      const top = mode === 'shared' ? edge : 3
      const raised = reclaimTitleColumns(rects, width - left, kh, 20 - top)
      if (raised !== rects) titleWidth = width
      rects = raised
    }
  }
  return { rects, mode, titleWidth }
}

/**
 * Place one cell (and, recursively, its children) at an absolute rect.
 *
 * `r` is in map coordinates. Mirrors `Treemap.cell()`: chain collapse first,
 * then the same box/label/edge math, then the gated child recursion whose
 * inner offset threads absolute coordinates down.
 */
function place<T>(
  node0: Node<T>,
  path0: T[],
  r: { x: number; y: number; w: number; h: number },
  depth: number,
  mode: Tiling,
  cfg: LayoutConfig<T>,
): PlacedCell<T> {
  const { getLabel, childrenOf, showLabels, collapseChains, borderWidth, edgeEmphasis } = cfg
  const folded = isFolded(node0)

  // Single-child wrapper chains collapse into one cell labeled `a/…/z`: the
  // cell IS the deepest node (its children + drill target), the chain recorded
  // in the path so crumbs/tooltips still show every level.
  let node = node0
  let path = path0
  let chainLabels: string[] | null = null
  if (collapseChains && !folded) {
    let cur = node0 as T
    let p = path0
    const labels = [getLabel(cur)]
    for (;;) {
      const only = childrenOf(cur, p)
      if (!only || only.length !== 1) break
      cur = only[0]
      p = [...p, cur]
      labels.push(getLabel(cur))
    }
    if (labels.length > 1) {
      node = cur
      path = p
      chainLabels = labels
    }
  }

  const dust = Math.min(r.w, r.h) < 14
  const shared = mode === 'shared'
  const showLbl = showLabels && r.w > 36 && r.h > 13
  const bw = shared
    ? Math.min(borderWidth(depth, { w: r.w, h: r.h }) * edgeEmphFactor(depth, edgeEmphasis), dust ? 1 : Infinity)
    : 0
  const edge = bw / 2
  const boxW = shared ? r.w : r.w - (dust ? 1 : 2)
  const boxH = shared ? r.h : r.h - (dust ? 1 : 2)

  const kidChildren = folded ? undefined : childrenOf(node as T, path)
  const { rects: kids, mode: kidsMode, titleWidth } = layoutChildren(node as T, path, chainLabels, kidChildren,
    { w: r.w, h: r.h, boxW, boxH, edge, showLbl, depth }, cfg)

  // Inner container offset (absolute) for the children region — matches the
  // DOM inner div's inset. Kids were laid at origin (0,0) inside it.
  const innerLeft = kidsMode === 'shared' ? edge : 3
  const innerTop = kidsMode === 'shared' ? (showLbl ? 20 : edge) : (showLbl ? 20 : 3)

  const children: PlacedCell<T>[] = kids
    .filter(s => s.w >= 3 && s.h >= 3)
    .map(s =>
      place(
        s.it,
        isFolded(s.it) ? path : [...path, s.it as T],
        { x: r.x + innerLeft + s.x, y: r.y + innerTop + s.y, w: s.w, h: s.h },
        depth + 1,
        kidsMode,
        cfg,
      ),
    )

  return {
    node,
    path,
    folded,
    chainLabels,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    depth,
    mode,
    edge,
    dust,
    boxW,
    boxH,
    showLbl,
    hasKids: children.length > 0,
    titleWidth,
    children,
  }
}

/**
 * Lay out a whole (sub)tree into an absolute-positioned placed-cell tree.
 *
 * `topRects` are the already-laid top-level rects (the component's `rects`,
 * which include its `foldThin` refold) so the top level isn't re-decided here;
 * this only recurses the nested levels. `basePath` ends at the viewed node.
 */
export function layoutCells<T>(
  topRects: Rect<Node<T>>[],
  basePath: T[],
  rootMode: Tiling,
  cfg: LayoutConfig<T>,
): PlacedCell<T>[] {
  return topRects
    .filter(r => r.w >= 3 && r.h >= 3)
    .map(r =>
      place(
        r.it,
        isFolded(r.it) ? basePath : [...basePath, r.it as T],
        r,
        0,
        rootMode,
        cfg,
      ),
    )
}

/** Flatten a placed tree parent-first (paint order: parents before children). */
export function flattenPlaced<T>(cells: PlacedCell<T>[], out: PlacedCell<T>[] = []): PlacedCell<T>[] {
  for (const c of cells) {
    out.push(c)
    if (c.children.length) flattenPlaced(c.children, out)
  }
  return out
}

/** Deepest cell whose rect contains (px, py), or null — the hit-test. */
export function hitTest<T>(cells: PlacedCell<T>[], px: number, py: number): PlacedCell<T> | null {
  let found: PlacedCell<T> | null = null
  for (const c of cells) {
    if (px >= c.x && px < c.x + c.w && py >= c.y && py < c.y + c.h) {
      found = c
      const deeper = hitTest(c.children, px, py)
      if (deeper) found = deeper
    }
  }
  return found
}
