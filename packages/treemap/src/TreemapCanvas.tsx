import { useLayoutEffect, useMemo, useRef } from 'react'
import { CONTAINER_BG, parseColor } from './colors'
import { colorResolver } from './cssColor'
import { drawDust } from './DustHatch'
import { resolveCellStyle, resolveRing, type StyleOpts } from './cellStyle'
import { flattenPlaced, hitTest, type FoldedNode, type PlacedCell } from './layout'
import { squarify } from './squarify'

/**
 * Whole-map canvas renderer for `<Treemap renderer="canvas">`.
 *
 * Paints the entire placed-cell tree to one `<canvas>` (parent-first, so
 * children draw over their container) and hit-tests the same retained tree —
 * one code path, one paint loop, no DOM node per cell. Interaction is not
 * re-implemented here: pointer hits resolve to `(node, path)` and are handed
 * straight back to `<Treemap>`'s drill/tooltip handlers, the identical path a
 * DOM cell's event takes.
 *
 * Parity is staged. This first cut draws base + faded bg + shared edge stroke +
 * dust tail + labels, and hit-tests cells (folded dust resolves to its specific
 * child, like the DOM tile). Segments (makeup stripes), chain-label rendering,
 * `cellHref` anchors, and the lazy-load overlay are still DOM-only.
 */

/** A resolved pointer hit: a real cell, or a specific child under a dust tile. */
export interface CanvasHit<T> {
  /** Always a real node — a folded tile resolves to the child under the cursor. */
  node: T
  path: T[]
  key: string
  drillable: boolean
  /** Whether this cell renders nested tiles (branch title-bar vs. leaf body). */
  branch: boolean
  /** This hit is a child resolved out of a dust tile (pins, never drills, and
   * bypasses `onCellClick` — matching the DOM dust tile). */
  foldChild: boolean
}

export interface TreemapCanvasProps<T> {
  cells: PlacedCell<T>[]
  width: number
  height: number
  styleOpts: StyleOpts<T>
  getSize: (n: T) => number
  getLabel: (n: T) => string
  formatSize: (n: number) => string
  /** Inline-size placement on a cell's first line (see `TreemapProps.sizeAlign`). */
  sizeAlign: 'left' | 'right'
  /** Min cell width for the inline size (see `TreemapProps.inlineSizeMinWidth`). */
  inlineSizeMinWidth: number
  idFor: (n: T, p: T[]) => string
  expandable: (n: T, p: T[]) => boolean
  dustTexture: boolean
  /** Per-cell link href for the a11y overlay (mirrors the DOM `cellHref`). */
  cellHref?: (n: T, path: T[]) => string | undefined
  /** Build the focusable a11y overlay (see `TreemapProps.a11yLinks`). */
  a11yLinks: boolean
  /** Cap on overlay elements, largest cells first. */
  a11yMaxCells: number
  /** Minimum short-side px for a cell to get an overlay element. */
  a11yMinSide: number
  /** Key of the currently pinned cell, ringed on the canvas so the pin reads. */
  pinnedKey: string | null
  /** Mirror of the internal `<canvas>` element, for image export (`exportable`). */
  canvasRef?: React.MutableRefObject<HTMLCanvasElement | null>
  onHover: (hit: CanvasHit<T>, clientX: number, clientY: number) => void
  onClick: (hit: CanvasHit<T>, e: React.MouseEvent) => void
  onLeave: () => void
}

const CONTAINER_RGB = `rgb(${CONTAINER_BG[0]}, ${CONTAINER_BG[1]}, ${CONTAINER_BG[2]})`
/** First (synchronous) frame's paint budget — small maps finish inside it. */
const SYNC_BUDGET_MS = 8
/** Each subsequent animation frame's paint budget, leaving headroom in 16ms. */
const FRAME_BUDGET_MS = 10

export interface PaintOpts<T> {
  styleOpts: StyleOpts<T>
  getSize: (n: T) => number
  getLabel: (n: T) => string
  formatSize: (n: number) => string
  sizeAlign: 'left' | 'right'
  /** Min cell width for the inline size (see `TreemapProps.inlineSizeMinWidth`). */
  inlineSizeMinWidth: number
  dustTexture: boolean
}

// The pass's `var()` resolver (see cssColor.ts): set by the paint effect
// against the canvas element, so a consumer's `var(--other)` / `var(--ink)`
// paint as the theme's color instead of `parseColor` giving up on them.
let resolveVar: (c: string) => string = c => c
// Label sizes, read from the same CSS custom properties the DOM renderer's
// labels use (`--dt-treemap-lbl-fs`, `--dt-treemap-lbl-fs-sm`), so the two
// renderers set type identically for a given consumer stylesheet.
let lblFs = 13.5
let lblFsSm = 11.5
function readLabelSizes(el: Element): void {
  const cs = getComputedStyle(el)
  const px = (v: string, dflt: number) => {
    const m = /^([\d.]+)(px|rem)?$/.exec(v.trim())
    if (!m) return dflt
    return m[2] === 'rem' ? parseFloat(m[1]) * parseFloat(getComputedStyle(document.documentElement).fontSize || '16') : parseFloat(m[1])
  }
  lblFs = px(cs.getPropertyValue('--dt-treemap-lbl-fs'), 13.5)
  lblFsSm = px(cs.getPropertyValue('--dt-treemap-lbl-fs-sm'), 11.5)
}

/** A CSS color → `rgba()` at `alpha`, or null if it isn't a parseable solid
 * (a gradient/`color-mix`) so the caller can skip or fall back. A `var()`
 * reference is resolved against the canvas first (see `resolveVar`). */
function rgbaAt(c: string | undefined, alpha: number): string | null {
  const p = parseColor(resolveVar(c ?? ''))
  if (!p) return null
  return `rgba(${p[0]}, ${p[1]}, ${p[2]}, ${p[3] * alpha})`
}

export function TreemapCanvas<T>({
  cells,
  width,
  height,
  styleOpts,
  getSize,
  getLabel,
  formatSize,
  sizeAlign,
  inlineSizeMinWidth,
  idFor,
  expandable,
  dustTexture,
  cellHref,
  a11yLinks,
  a11yMaxCells,
  a11yMinSide,
  pinnedKey,
  canvasRef,
  onHover,
  onClick,
  onLeave,
}: TreemapCanvasProps<T>) {
  const ref = useRef<HTMLCanvasElement>(null)
  // Callback ref that keeps both the internal ref (used by the paint effect)
  // and the consumer's export mirror pointed at the live element.
  const setCanvas = (el: HTMLCanvasElement | null) => {
    ref.current = el
    if (canvasRef) canvasRef.current = el
  }
  // Biggest-first paint order. A container's rect always contains its
  // descendants', so its area strictly exceeds theirs — area-descending is
  // therefore a valid ancestor-before-descendant order (children paint over
  // their parent) *and* makes the first frame the whole map's skeleton, refined
  // by later frames. The tree (`cells`) still hit-tests deepest-first.
  const flat = useMemo(() => {
    const f = flattenPlaced(cells)
    f.sort((a, b) => b.w * b.h - a.w * a.h)
    return f
  }, [cells])

  // Progressive paint: the first frame paints synchronously up to a time budget
  // — small/medium maps finish here (identical to a one-shot paint, and correct
  // in a backgrounded tab where rAF is paused) — and any remainder streams in
  // over subsequent animation frames, so a 1e4–1e5-cell map never blocks the
  // main thread. The canvas is never cleared between chunks (they accumulate);
  // a layout/size/style change re-runs the effect, which clears and restarts.
  useLayoutEffect(() => {
    const cv = ref.current
    if (!cv || width <= 0 || height <= 0) return
    const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
    cv.width = Math.round(width * dpr)
    cv.height = Math.round(height * dpr)
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    // Map ground = container color; gaps gutters and empty space show through it.
    ctx.clearRect(0, 0, width, height)
    ctx.fillStyle = CONTAINER_RGB
    ctx.fillRect(0, 0, width, height)

    // Fresh per pass so a theme toggle repaints correctly (see cssColor.ts) and
    // label sizes track the consumer's current stylesheet.
    resolveVar = colorResolver(cv)
    readLabelSizes(cv)
    const opts: PaintOpts<T> = { styleOpts, getSize, getLabel, formatSize, sizeAlign, inlineSizeMinWidth, dustTexture }
    let i = 0
    let raf = 0
    // Paint until `budgetMs` elapses (clock read every 256 cells to keep it
    // cheap) or the list is exhausted.
    const paintUntil = (budgetMs: number) => {
      const start = performance.now()
      let n = 0
      while (i < flat.length) {
        paintCell(ctx, flat[i], opts)
        i++
        if (++n >= 256) { n = 0; if (performance.now() - start >= budgetMs) return }
      }
    }
    paintUntil(SYNC_BUDGET_MS)
    const step = () => {
      paintUntil(FRAME_BUDGET_MS)
      if (i < flat.length) raf = requestAnimationFrame(step)
    }
    if (i < flat.length) raf = requestAnimationFrame(step)
    return () => { if (raf) cancelAnimationFrame(raf) }
  }, [flat, width, height, styleOpts, getSize, getLabel, formatSize, sizeAlign, inlineSizeMinWidth, dustTexture])

  /** Squarify a folded tile's children over its box, to resolve a dust hover. */
  const dustChildAt = (
    cell: PlacedCell<T>,
    lx: number,
    ly: number,
  ): { node: T; path: T[]; key: string } | null => {
    const fn = cell.node as FoldedNode<T>
    if (!dustTexture || fn.children.length <= 1 || fn.children.length > 4000) return null
    if (Math.min(cell.w, cell.h) < 10) return null
    const rects = squarify<T>(fn.children, 0, 0, cell.boxW, cell.boxH, getSize)
    const rx = lx - cell.x
    const ry = ly - cell.y
    const hit = rects.find(rc => rx >= rc.x && rx < rc.x + rc.w && ry >= rc.y && ry < rc.y + rc.h)
    if (!hit) return null
    const path = [...cell.path, hit.it]
    return { node: hit.it, path, key: idFor(hit.it, path) }
  }

  /** CanvasHit for a real (non-folded) cell — shared by hit-test and overlay. */
  const cellHit = (cell: PlacedCell<T>): CanvasHit<T> => {
    const node = cell.node as T
    return {
      node,
      path: cell.path,
      key: idFor(node, cell.path),
      drillable: expandable(node, cell.path),
      branch: cell.hasKids,
      foldChild: false,
    }
  }

  const toHit = (cell: PlacedCell<T>, lx: number, ly: number): CanvasHit<T> | null => {
    if (cell.folded) {
      const child = dustChildAt(cell, lx, ly)
      if (!child) return null
      return { node: child.node, path: child.path, key: child.key, drillable: false, branch: false, foldChild: true }
    }
    return cellHit(cell)
  }

  const localXY = (e: React.MouseEvent) => {
    const box = (e.currentTarget as HTMLElement).getBoundingClientRect()
    return { lx: e.clientX - box.left, ly: e.clientY - box.top }
  }

  // A11y overlay set: the largest labeled/container cells (flat is area-sorted),
  // above the min-side floor, capped — a bounded DOM mirror of the canvas for
  // keyboard focus, screen readers, Vimium, `cellHref` links, and crawlers.
  const overlayCells = useMemo(() => {
    if (!a11yLinks) return []
    const out: PlacedCell<T>[] = []
    for (const c of flat) {
      if (out.length >= a11yMaxCells) break
      if (c.folded) continue
      if (!(c.showLbl || c.hasKids)) continue
      if (Math.min(c.w, c.h) < a11yMinSide) continue
      out.push(c)
    }
    return out
  }, [flat, a11yLinks, a11yMaxCells, a11yMinSide])

  // The pinned cell (if any), so the canvas can ring it — otherwise a pin is
  // invisible on the canvas and the map looks frozen (hover is suppressed).
  const pinnedCell = useMemo(
    () => (pinnedKey ? flat.find(c => !c.folded && idFor(c.node as T, c.path) === pinnedKey) ?? null : null),
    [flat, pinnedKey, idFor],
  )

  return (
    <>
      <canvas
        ref={setCanvas}
        width={Math.max(1, Math.round(width))}
        height={Math.max(1, Math.round(height))}
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', cursor: 'default' }}
        onMouseMove={e => {
          const cv = e.currentTarget
          const { lx, ly } = localXY(e)
          const cell = hitTest(cells, lx, ly)
          // No-hit (a gutter between cells): leave the current tip alone, matching
          // the DOM renderer where sweeping a gap doesn't clear. Real exit clears.
          if (!cell) { cv.style.cursor = 'default'; return }
          const hit = toHit(cell, lx, ly)
          if (!hit) { cv.style.cursor = 'default'; return }
          // Interactive cells read as clickable, like the DOM renderer's cells.
          const interactive = hit.drillable || !!(cellHref && cellHref(hit.node, hit.path))
          cv.style.cursor = interactive ? 'pointer' : 'default'
          // Anchor the tip to the cell's top-left (screen), so it lands in the
          // same place no matter which edge the pointer entered from — the
          // previous entry-cursor anchor made the placement look random.
          const box = cv.getBoundingClientRect()
          onHover(hit, box.left + cell.x, box.top + cell.y)
        }}
        onMouseLeave={onLeave}
        onClick={e => {
          const { lx, ly } = localXY(e)
          const cell = hitTest(cells, lx, ly)
          if (!cell) return
          const hit = toHit(cell, lx, ly)
          if (hit) onClick(hit, e)
        }}
      />
      {/* Pinned-cell ring — the visible counterpart of the pinned tooltip's ×,
          so it's clear which cell is pinned (and why hover is suppressed). */}
      {pinnedCell && (
        <div
          style={{
            position: 'absolute',
            left: pinnedCell.x, top: pinnedCell.y, width: pinnedCell.w, height: pinnedCell.h,
            boxSizing: 'border-box',
            border: '2px solid var(--dt-treemap-pin, rgba(120, 170, 255, 0.95))',
            borderRadius: pinnedCell.mode === 'shared' ? 0 : 2,
            pointerEvents: 'none',
          }}
        />
      )}
      {/* A11y/keyboard/link overlay. Transparent and pointer-events:none — the
          canvas above handles all mouse input; these focusable anchors/buttons
          exist for keyboard, screen readers, Vimium, real `cellHref` links, and
          crawlers. Focus scrubs (fires the same hover); Enter/programmatic
          click routes through the same drill/pin path as a mouse click. */}
      {overlayCells.length > 0 && (
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
          {overlayCells.map(cell => {
            const node = cell.node as T
            const href = cellHref?.(node, cell.path)
            const hit = cellHit(cell)
            // Transparent (not opacity:0 — that hides the focus ring and can
            // make Vimium skip it); invisible until focused, when the browser
            // rings the cell. pointer-events:none so all mouse input hits the
            // canvas below.
            const commonStyle = {
              position: 'absolute' as const,
              left: cell.x, top: cell.y, width: cell.w, height: cell.h,
              margin: 0, padding: 0, border: 0, background: 'transparent',
              color: 'transparent', font: 'inherit', textAlign: 'start' as const,
              pointerEvents: 'none' as const, overflow: 'hidden',
            }
            const ariaLabel = `${getLabel(node)}, ${formatSize(getSize(node))}`
            const focusHover = () => {
              const r = ref.current?.getBoundingClientRect()
              onHover(hit, (r?.left ?? 0) + cell.x + cell.w / 2, (r?.top ?? 0) + cell.y + 8)
            }
            const activate = (e: React.SyntheticEvent) => {
              const me = e as unknown as React.MouseEvent
              // Anchors: let modified/middle clicks do native new-tab; otherwise
              // suppress navigation and route through the SPA drill/click path.
              if (!(href && (me.metaKey || me.ctrlKey || me.shiftKey || me.altKey || me.button === 1))) {
                e.preventDefault()
                onClick(hit, me)
              }
            }
            const onKeyDown = (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') activate(e)
            }
            return href
              ? <a key={hit.key} className="dt-treemap-a11y-cell" href={href} aria-label={ariaLabel} style={commonStyle} onFocus={focusHover} onClick={activate} onKeyDown={onKeyDown} />
              : <button key={hit.key} className="dt-treemap-a11y-cell" type="button" aria-label={ariaLabel} style={commonStyle} onFocus={focusHover} onClick={activate} onKeyDown={onKeyDown} />
          })}
        </div>
      )}
    </>
  )
}

/**
 * Paint the whole placed-cell tree to `canvas` in one synchronous pass (no
 * progressive budget) — the export path, and any non-progressive render. Sizes
 * the canvas to `width`×`height` at `devicePixelRatio`, and resolves theme CSS
 * vars + label sizes against `resolveEl` (an in-DOM, themed element — an
 * offscreen export canvas can't resolve a consumer's `var(--…)`). Lets the DOM
 * renderer export an identical canvas rendering without mounting one.
 */
export function renderMapToCanvas<T>(
  canvas: HTMLCanvasElement,
  cells: PlacedCell<T>[],
  width: number,
  height: number,
  opts: PaintOpts<T>,
  resolveEl: Element,
): void {
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
  canvas.width = Math.max(1, Math.round(width * dpr))
  canvas.height = Math.max(1, Math.round(height * dpr))
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)
  ctx.fillStyle = CONTAINER_RGB
  ctx.fillRect(0, 0, width, height)
  resolveVar = colorResolver(resolveEl)
  readLabelSizes(resolveEl)
  const flat = flattenPlaced(cells)
  flat.sort((a, b) => b.w * b.h - a.w * a.h)
  for (const c of flat) paintCell(ctx, c, opts)
}

/**
 * Paint one placed cell: opaque base, faded bg layer, shared edge stroke, dust
 * hatch, and label — the canvas equivalent of one DOM `.dt-treemap-cell`.
 * Standalone so the progressive loop can call it per cell across frames.
 */
function paintCell<T>(ctx: CanvasRenderingContext2D, cell: PlacedCell<T>, o: PaintOpts<T>): void {
  const { styleOpts, getSize, getLabel, formatSize, sizeAlign, inlineSizeMinWidth, dustTexture } = o
  const { x, y, w, h, depth, mode, edge, dust, folded, showLbl, hasKids } = cell
  const shared = mode === 'shared'
  // Cell box: gaps leaves a 2px (1px dust) gutter to the container ground;
  // shared fills the exact rect.
  const cw = shared ? w : w - (dust ? 1 : 2)
  const ch = shared ? h : h - (dust ? 1 : 2)
  if (cw <= 0 || ch <= 0) return

  const { style, builtinEdge } = resolveCellStyle(cell, styleOpts)
  const fade = styleOpts.fadeAt(depth)
  const alpha = fade * (style.opacity ?? 1)

  // Opaque base under the translucent bg layer (matches the DOM cell's
  // container-color base): so a faded bg recedes toward the container tone.
  ctx.fillStyle = CONTAINER_RGB
  ctx.fillRect(x, y, cw, ch)

  // Faded bg layer, inset by the half-stroke in shared mode.
  const inset = shared ? edge : 0
  const bx = x + inset
  const by = y + inset
  const bw = cw - 2 * inset
  const bh = ch - 2 * inset
  if (bw > 0 && bh > 0) {
    const fill = rgbaAt(style.bg, alpha)
    if (fill) {
      ctx.fillStyle = fill
      ctx.fillRect(bx, by, bw, bh)
    }
  }

  // Shared edge: this cell's half-stroke ring (contrast default or pinned).
  if (shared && edge > 0) {
    const stroke = style.edge ? rgbaAt(style.edge, 1) : builtinEdge ? rgbaAt(builtinEdge, 1) : null
    if (stroke) {
      ctx.strokeStyle = stroke
      ctx.lineWidth = edge
      ctx.strokeRect(x + edge / 2, y + edge / 2, cw - edge, ch - edge)
    }
  }

  // Dust tail: dashed frame + the tightening cross-hatch.
  if (folded && dustTexture && Math.min(w, h) >= 6) {
    if (Math.min(w, h) >= 8) {
      ctx.strokeStyle = 'rgba(150, 150, 165, 0.55)'
      ctx.lineWidth = 1
      ctx.setLineDash([2, 2])
      ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1)
      ctx.setLineDash([])
    }
    drawDust(ctx, bx, by, bw, bh, (cell.node as FoldedNode<T>).count)
  }

  // Makeup stripes: a leaf cell with a mixed composition renders proportional
  // inset slices along its longer axis instead of one blob — the `bg` frame
  // showing through the inset + the outer border reads as "one blob split by
  // share", distinct from real child tiles. Faded with the bg layer.
  const segs = style.segments
  if (segs && segs.length > 1 && !hasKids && !dust && Math.min(w, h) >= 18) {
    const segInset = shared ? Math.max(1, 2 * edge) : 3
    const gap = 1
    const horiz = w >= h // slice along the longer axis
    const span = (horiz ? cw : ch) - 2 * segInset - gap * (segs.length - 1)
    if (span >= segs.length * 2) {
      const total = segs.reduce((s, x) => s + x.frac, 0) || 1
      let at = segInset
      for (const seg of segs) {
        const len = (seg.frac / total) * span
        const fill = rgbaAt(seg.color, alpha)
        if (fill) {
          ctx.fillStyle = fill
          ctx.beginPath()
          if (horiz) roundRect(ctx, x + at, y + segInset, len, ch - 2 * segInset - 2, 2)
          else roundRect(ctx, x + segInset, y + at, cw - 2 * segInset - 2, len, 2)
          ctx.fill()
        }
        at += len + gap
      }
    }
  }

  // Label: name (+ inline size when there's room). Ink stays full-strength.
  // Mirrors the DOM `.dt-treemap-lbl` / `.dt-treemap-lbl2` rules exactly:
  // branch bars and short leaves (h ≤ 34) show the size inline when the cell
  // is wider than 90px; a tall leaf drops it to a second line, whatever its
  // width (the label itself already needs w > 36).
  if (showLbl) {
    const ink = rgbaAt(style.ink, 1) ?? 'rgba(230, 230, 238, 1)'
    const fs = w < 64 ? lblFsSm : lblFs
    ctx.font = `${fs}px system-ui, -apple-system, sans-serif`
    ctx.textBaseline = 'top'
    ctx.fillStyle = ink
    const label = folded
      ? `(+${(cell.node as FoldedNode<T>).count})`
      : cell.chainLabels
        ? (cell.chainLabels.length > 3
            ? `${cell.chainLabels[0]}/…/${cell.chainLabels[cell.chainLabels.length - 1]}`
            : cell.chainLabels.join('/'))
        : getLabel(cell.node as T)
    // Clip text to the cell so long names don't bleed across neighbors.
    ctx.save()
    ctx.beginPath()
    ctx.rect(x, y, cw, ch)
    ctx.clip()
    const pad = 4
    const kidSize = folded ? (cell.node as FoldedNode<T>).size : getSize(cell.node as T)
    // Same rule as the DOM label: inline size on branch bars / short leaves
    // wider than the consumer's threshold.
    const inlineSize = (hasKids || h <= 34) && w > inlineSizeMinWidth
    // Same 6px name↔size gap as the DOM label's flex `gap`.
    const gap = 6
    let nameMax = cw - 2 * pad
    const szText = inlineSize ? formatSize(kidSize) : ''
    const szW = inlineSize ? ctx.measureText(szText).width : 0
    if (inlineSize) nameMax -= szW + gap
    const name = fit(ctx, label, Math.max(0, nameMax))
    ctx.fillText(name, x + pad, y + 2)
    if (inlineSize) {
      // `left`: right after the (possibly ellipsized) name; `right`: flush
      // with the cell's far edge — the DOM's `margin-left: auto`.
      const sx = sizeAlign === 'right'
        ? x + cw - pad - szW
        : x + pad + ctx.measureText(name).width + gap
      ctx.globalAlpha = 0.75
      ctx.fillText(szText, sx, y + 2)
      ctx.globalAlpha = 1
    }
    // Second-line size for a tall leaf (name owns the first line).
    if (!hasKids && h > 34) {
      ctx.globalAlpha = 0.75
      ctx.font = `${lblFsSm}px system-ui, -apple-system, sans-serif`
      ctx.fillText(fit(ctx, formatSize(kidSize), cw - 2 * pad), x + pad, y + 2 + fs + 3)
      ctx.globalAlpha = 1
    }
    ctx.restore()
  }

  // Consumer emphasis ring (brush/selection): the canvas counterpart of the DOM
  // cell's `ring` box-shadow. Drawn last so it frames over fill + label, in
  // either tiling mode; a stroked rounded-rect matching the cell's corner radius.
  const ring = resolveRing(style.ring)
  if (ring && ring.width > 0) {
    const off = ring.inset ? ring.width / 2 : -ring.width / 2
    const rw = cw - 2 * off
    const rh = ch - 2 * off
    if (rw > 0 && rh > 0) {
      ctx.strokeStyle = ring.color
      ctx.lineWidth = ring.width
      ctx.beginPath()
      roundRect(ctx, x + off, y + off, rw, rh, shared ? 0 : dust ? 1.5 : 3)
      ctx.stroke()
    }
  }
}

/** Add a rounded-rect subpath (falls back to a plain rect where unsupported). */
function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  if (w <= 0 || h <= 0) return
  if (typeof ctx.roundRect === 'function') ctx.roundRect(x, y, w, h, Math.min(r, w / 2, h / 2))
  else ctx.rect(x, y, w, h)
}

/** Truncate `text` with an ellipsis to fit `maxW` px in the current ctx font. */
function fit(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (maxW <= 0) return ''
  if (ctx.measureText(text).width <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxW) lo = mid
    else hi = mid - 1
  }
  return lo > 0 ? text.slice(0, lo) + '…' : ''
}
