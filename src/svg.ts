/**
 * DOM-free static treemap: the same layout (`layoutCells`) and cell styling
 * (`resolveCellStyle`) as `<Treemap>`, painted to an SVG string instead of
 * DOM nodes or a canvas. For server-side images (Workers + resvg, Node): no
 * `window`, no `document`, no React.
 *
 * `staticLayout` mirrors the component's default layout pass (area fold, plain
 * squarify, the top-level thin-sliver refold, one tiling mode); `treemapSvg`
 * mirrors `TreemapCanvas`'s `paintCell` (opaque base, faded bg, shared edge
 * half-stroke, dust frame, name + inline size or a second-line size). Text is
 * fitted with a caller-supplied `measure`, since there's no canvas to ask.
 */
import { CONTAINER_BG, DEFAULT_PALETTE, parseColor, slotColor } from './colors'
import { resolveCellStyle, resolveRing, type StyleOpts } from './cellStyle'
import { flattenPlaced, isFolded, layoutCells, type FoldedNode, type LayoutConfig, type PlacedCell } from './layout'
import { foldSmall, foldThin, squarify } from './squarify'
import type { Tiling } from './cellTypes'

type Node<T> = T | FoldedNode<T>

export interface StaticLayoutOpts<T> {
  width: number
  height: number
  getSize: (n: T) => number
  getLabel: (n: T) => string
  getChildren: (n: T) => T[] | undefined
  /** `<Treemap>`'s defaults unless given. */
  collapseChains?: boolean
  showLabels?: boolean
  minCellArea?: number | null
  minCellSide?: number | null
  tiling?: Tiling
  borderWidth?: (depth: number, ctx: { w: number; h: number }) => number
  edgeEmphasis?: number
}

const defaultBorderWidth = (depth: number) => Math.max(1, 2 - depth)

/** The placed-cell tree for `root`'s children at `width`×`height`, as `<Treemap>` lays it. */
export function staticLayout<T>(root: T, o: StaticLayoutOpts<T>): PlacedCell<T>[] {
  const { width, height, getSize, getLabel, getChildren } = o
  const minCellArea = o.minCellArea === undefined ? 16 : o.minCellArea
  const minCellSide = o.minCellSide === undefined ? 7 : o.minCellSide
  const tiling = o.tiling ?? 'gaps'
  const sz = (n: Node<T>) => (isFolded(n) ? n.size : getSize(n))
  const mergeItems = (small: Node<T>[]): Node<T> => {
    const flat: T[] = []
    let sum = 0
    for (const s of small) {
      if (isFolded(s)) { flat.push(...s.children); sum += s.size } else { flat.push(s); sum += getSize(s) }
    }
    return { __folded: true, count: flat.length, size: sum, children: flat }
  }
  const fold = (raw: Node<T>[], w: number, h: number): Node<T>[] =>
    minCellArea == null ? raw : foldSmall<Node<T>>(raw, w, h, sz, mergeItems, minCellArea)
  const layTiles = (items: Node<T>[], x: number, y: number, w: number, h: number) => squarify<Node<T>>(items, x, y, w, h, sz)
  let rects = layTiles(fold((getChildren(root) ?? []).slice(), width, height), 0, 0, width, height)
  if (minCellSide != null) {
    const refolded = foldThin<Node<T>>(rects, minCellSide, mergeItems)
    if (refolded) rects = squarify<Node<T>>(refolded, 0, 0, width, height, sz)
  }
  const cfg: LayoutConfig<T> = {
    getSize, getLabel,
    childrenOf: n => getChildren(n),
    showLabels: o.showLabels ?? true,
    collapseChains: o.collapseChains ?? false,
    borderWidth: o.borderWidth ?? defaultBorderWidth,
    edgeEmphasis: o.edgeEmphasis ?? 0,
    fold, layTiles,
    tilingFor: () => tiling,
  }
  return layoutCells(rects, [root], tiling, cfg)
}

export interface SvgPaintOpts<T> {
  width: number
  height: number
  getSize: (n: T) => number
  getLabel: (n: T) => string
  formatSize: (n: number) => string
  /** A cell's size text, overriding `formatSize(getSize(n))` (e.g. a signed change). */
  formatCell?: (n: T) => string
  colorForCell?: StyleOpts<T>['colorForCell']
  /** Text width in px of `text` at `px` font size (the SVG's font). */
  measure: (text: string, px: number) => number
  /** `var(--name)` values for colors the styles reference (a consumer's theme);
   *  an unlisted var falls back to its `var(--name, fallback)` fallback. */
  vars?: Record<string, string>
  fontFamily?: string
  /** Label sizes, px (`--dt-treemap-lbl-fs` / `-sm`). */
  labelPx?: number
  labelPxSm?: number
  depthFade?: number
  rootFade?: number
  fadeFloor?: number
  edgeContrast?: boolean
  dustTexture?: boolean
  sizeAlign?: 'left' | 'right'
  inlineSizeMinWidth?: number
}

const r1 = (n: number) => Math.round(n * 100) / 100

export function escXml(s: string): string {
  return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!)
}

/** Substitute `var(--x[, fallback])` from `vars` (recursively), so the color
 * parser sees a literal. */
export function resolveVars(c: string, vars: Record<string, string>): string {
  let out = c
  for (let i = 0; i < 8 && out.includes('var('); i++) {
    out = out.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)/g, (_m, name: string, fb?: string) => vars[name] ?? fb?.trim() ?? '')
  }
  return out
}

/** `fill="rgb()" fill-opacity=".."` for a CSS color at `alpha` (resvg wants
 * plain rgb + a separate opacity), or null for an unparseable one. */
function paint(attr: 'fill' | 'stroke', c: string | undefined, alpha: number, vars: Record<string, string>): string | null {
  const p = parseColor(resolveVars(c ?? '', vars))
  if (!p) return null
  const a = p[3] * alpha
  return `${attr}="rgb(${p[0]},${p[1]},${p[2]})"${a < 1 ? ` ${attr}-opacity="${r1(a)}"` : ''}`
}

function fitText(text: string, maxW: number, px: number, measure: (t: string, px: number) => number): string {
  if (maxW <= 0) return ''
  if (measure(text, px) <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (measure(text.slice(0, mid) + '…', px) <= maxW) lo = mid
    else hi = mid - 1
  }
  return lo > 0 ? text.slice(0, lo) + '…' : ''
}

/** The map as SVG elements (no `<svg>` wrapper), origin at (0, 0): a `<g>`
 * a caller can translate into a larger image. */
export function treemapSvg<T>(cells: PlacedCell<T>[], o: SvgPaintOpts<T>): string {
  const vars = o.vars ?? {}
  const family = o.fontFamily ?? 'system-ui, sans-serif'
  const fs = o.labelPx ?? 13.5
  const fsSm = o.labelPxSm ?? 11.5
  const depthFade = o.depthFade ?? 0.82
  const rootFade = o.rootFade ?? 0.92
  const fadeFloor = o.fadeFloor ?? 0.75
  const sizeAlign = o.sizeAlign ?? 'left'
  const inlineMin = o.inlineSizeMinWidth ?? 90
  const dustTexture = o.dustTexture ?? true
  const topKids = cells.map(c => c.node)
  const styleOpts: StyleOpts<T> = {
    colorForCell: o.colorForCell,
    getLabel: o.getLabel,
    topLevelSlot: new Map(topKids.filter(n => !isFolded(n)).map((k, i) => [o.getLabel(k as T), slotColor(i, DEFAULT_PALETTE)])),
    defaultSlots: DEFAULT_PALETTE,
    dustTexture,
    edgeContrast: o.edgeContrast ?? true,
    fadeAt: d => Math.max(rootFade * depthFade ** d, fadeFloor),
  }
  const base = `fill="rgb(${CONTAINER_BG.join(',')})"`
  const out: string[] = [`<g font-family="${escXml(family)}">`, `<rect width="${r1(o.width)}" height="${r1(o.height)}" ${base}/>`]
  const flat = flattenPlaced(cells).sort((a, b) => b.w * b.h - a.w * a.h)
  const text = (x: number, y: number, s: string, px: number, ink: string, alpha = 1) =>
    `<text x="${r1(x)}" y="${r1(y + px * 0.8)}" font-size="${px}" ${ink}${alpha < 1 ? ` opacity="${alpha}"` : ''}>${escXml(s)}</text>`
  for (const cell of flat) {
    const { x, y, w, h, depth, mode, edge, dust, folded, showLbl, hasKids } = cell
    const shared = mode === 'shared'
    const cw = shared ? w : w - (dust ? 1 : 2)
    const ch = shared ? h : h - (dust ? 1 : 2)
    if (cw <= 0 || ch <= 0) continue
    const { style, builtinEdge } = resolveCellStyle(cell, styleOpts)
    const alpha = styleOpts.fadeAt(depth) * (style.opacity ?? 1)
    out.push(`<rect x="${r1(x)}" y="${r1(y)}" width="${r1(cw)}" height="${r1(ch)}" ${base}/>`)
    const inset = shared ? edge : 0
    const [bx, by, bw, bh] = [x + inset, y + inset, cw - 2 * inset, ch - 2 * inset]
    if (bw > 0 && bh > 0) {
      const fill = paint('fill', style.bg, alpha, vars)
      if (fill) out.push(`<rect x="${r1(bx)}" y="${r1(by)}" width="${r1(bw)}" height="${r1(bh)}" ${fill}/>`)
    }
    if (shared && edge > 0) {
      const stroke = paint('stroke', style.edge ?? builtinEdge ?? undefined, 1, vars)
      if (stroke) out.push(`<rect x="${r1(x + edge / 2)}" y="${r1(y + edge / 2)}" width="${r1(cw - edge)}" height="${r1(ch - edge)}" fill="none" ${stroke} stroke-width="${r1(edge)}"/>`)
    }
    if (folded && dustTexture && Math.min(w, h) >= 8) {
      out.push(`<rect x="${r1(bx + 0.5)}" y="${r1(by + 0.5)}" width="${r1(bw - 1)}" height="${r1(bh - 1)}" fill="none" stroke="rgb(150,150,165)" stroke-opacity="0.55" stroke-dasharray="2 2"/>`)
    }
    if (showLbl) {
      const ink = paint('fill', style.ink, 1, vars) ?? 'fill="rgb(230,230,238)"'
      const px = w < 64 ? fsSm : fs
      const label = folded
        ? `(+${(cell.node as FoldedNode<T>).count})`
        : cell.chainLabels
          ? (cell.chainLabels.length > 3
              ? `${cell.chainLabels[0]}/…/${cell.chainLabels[cell.chainLabels.length - 1]}`
              : cell.chainLabels.join('/'))
          : o.getLabel(cell.node as T)
      const pad = 4
      const gap = 6
      const kidSize = folded ? (cell.node as FoldedNode<T>).size : o.getSize(cell.node as T)
      const inlineSize = (hasKids || h <= 34) && w > inlineMin
      const sizeText = folded || !o.formatCell ? o.formatSize(kidSize) : o.formatCell(cell.node as T)
      const szText = inlineSize ? sizeText : ''
      const szW = inlineSize ? o.measure(szText, px) : 0
      const nameMax = cw - 2 * pad - (inlineSize ? szW + gap : 0)
      const name = fitText(label, Math.max(0, nameMax), px, o.measure)
      if (ch >= px + 2 && name) {
        out.push(text(x + pad, y + 2, name, px, ink))
        if (inlineSize) {
          const sx = sizeAlign === 'right' ? x + cw - pad - szW : x + pad + o.measure(name, px) + gap
          out.push(text(sx, y + 2, szText, px, ink, 0.75))
        }
      }
      if (!hasKids && h > 34 && ch >= 2 + px + 3 + fsSm) {
        const second = fitText(sizeText, cw - 2 * pad, fsSm, o.measure)
        if (second) out.push(text(x + pad, y + 2 + px + 3, second, fsSm, ink, 0.75))
      }
    }
    const ring = resolveRing(style.ring)
    if (ring && ring.width > 0) {
      const off = ring.inset ? ring.width / 2 : -ring.width / 2
      const stroke = paint('stroke', ring.color, 1, vars)
      if (stroke && cw - 2 * off > 0 && ch - 2 * off > 0) {
        out.push(`<rect x="${r1(x + off)}" y="${r1(y + off)}" width="${r1(cw - 2 * off)}" height="${r1(ch - 2 * off)}" fill="none" ${stroke} stroke-width="${ring.width}"/>`)
      }
    }
  }
  out.push('</g>')
  return out.join('')
}
