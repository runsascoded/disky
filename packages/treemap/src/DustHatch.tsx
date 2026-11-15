import { useLayoutEffect, useRef } from 'react'
import { CONTAINER_BG } from './colors'
import { browserRgba, colorResolver } from './cssColor'
import { type DustNotch, dustInk, dustRules } from './dust'
import type { CellStyle } from './cellTypes'

export { dustLineCount, dustOffsets } from './dust'

/**
 * A canvas cross-hatch that stands in for a folded "(other)" tile — the long
 * tail of cells too small to draw as their own rects. Instead of one flat grey
 * blob, it draws horizontal + vertical rules whose spacing *tightens toward the
 * lower-right*, evoking the continued shrink of the dust it represents, and
 * whose count scales with how many real cells were folded in (denser hatch =
 * more hidden items). Inked black or white, whichever contrasts with the
 * ground it's drawn on.
 *
 * Pure paint — hit-detection over the region lives in the consumer (the
 * treemap maps a cursor position back to a specific folded child).
 */

export interface DustHatchProps {
  /** CSS px width of the region. */
  w: number
  /** CSS px height of the region. */
  h: number
  /** How many real cells were folded in — drives hatch density. */
  count: number
  /** Rule color. Default: black or white, whichever contrasts with `ground`
   *  painted at `alpha` over `--dt-treemap-container-bg` (resolved on the
   *  canvas, `var()`s and `color-mix()` included). */
  color?: string
  /** The cell style the hatch sits on (its largest band / segment, else `bg`). Default: none (the container ground). */
  ground?: CellStyle
  /** Also stroke a dashed frame in the same ink, marking a folded tile as not-a-real-cell. Default: false. */
  frame?: boolean
  /** Keep the rules off the label text of the `.dt-treemap-cell` holding this canvas (`<Treemap>`'s DOM cells),
   *  measured after layout (`dustRules`' notch), so none strikes through the name. Default: false. */
  clearLabels?: boolean
  /** The opacity `ground` is painted at (the depth fade × the cell's `opacity`). Default: 1. */
  alpha?: number
  /**
   * Rules per axis at `count = 1`, before the count scaling. The axis with
   * more room gets proportionally more. Default: 3.
   */
  baseLines?: number
  /** Geometric growth of the gap moving away from the dense (lower-right)
   * corner. >1. Default: 1.32. */
  ratio?: number
}

/**
 * Stroke the dust cross-hatch into an already-configured 2D context, at CSS-px
 * origin `(ox, oy)`. Self-contained (sets stroke style + width, does its own
 * `beginPath`/`stroke`), so both the standalone `<DustHatch>` canvas and the
 * whole-map canvas renderer draw an identical tile. `ctx` is assumed already
 * dpr-scaled by the caller; coordinates are CSS px. `color` defaults to the
 * light ink, for the dark container ground.
 */
export function drawDust(
  ctx: CanvasRenderingContext2D,
  ox: number,
  oy: number,
  w: number,
  h: number,
  count: number,
  color = dustInk({}),
  baseLines = 3,
  ratio = 1.32,
  notch?: DustNotch | null,
): void {
  const { v, h: hz } = dustRules(w, h, count, notch, baseLines, ratio)
  if (!v.length && !hz.length) return
  ctx.strokeStyle = color
  ctx.lineWidth = 1
  ctx.beginPath()
  for (const [x, y0] of v) {
    ctx.moveTo(ox + x, oy + y0)
    ctx.lineTo(ox + x, oy + h)
  }
  for (const [y, x0] of hz) {
    ctx.moveTo(ox + x0, oy + y)
    ctx.lineTo(ox + w, oy + y)
  }
  ctx.stroke()
}

/** The folded tile's dashed frame (not-a-real-cell), 1px inside the `w`×`h` box at `(ox, oy)`, in the hatch's ink. */
export function drawDustFrame(ctx: CanvasRenderingContext2D, ox: number, oy: number, w: number, h: number, color: string): void {
  if (w <= 1 || h <= 1) return
  ctx.strokeStyle = color
  ctx.lineWidth = 1
  ctx.setLineDash([2, 2])
  ctx.strokeRect(ox + 0.5, oy + 0.5, w - 1, h - 1)
  ctx.setLineDash([])
}

/** The text extent of the labels of `cv`'s `.dt-treemap-cell`, in `cv`'s CSS px (`w` wide), plus a 2px margin:
 *  the hatch's notch. Null when the cell has no label. */
function labelNotch(cv: HTMLCanvasElement, w: number): DustNotch | null {
  const labels = cv.closest('.dt-treemap-cell')?.querySelectorAll(':scope > .dt-treemap-lbl, :scope > .dt-treemap-lbl2')
  if (!labels?.length) return null
  const box = cv.getBoundingClientRect()
  if (!box.width) return null
  const k = w / box.width
  const range = document.createRange()
  let right = 0
  let bottom = 0
  for (const el of labels) {
    range.selectNodeContents(el)
    const r = range.getBoundingClientRect()
    if (!r.width) continue
    right = Math.max(right, r.right)
    bottom = Math.max(bottom, r.bottom)
  }
  return right ? { w: (right - box.left) * k + 2, h: (bottom - box.top) * k + 2 } : null
}

/** `ground`'s hatch ink as painted on `el`: its colors and the container ground resolved there. */
export function resolvedDustInk(el: Element, ground: CellStyle = {}, alpha = 1): string {
  const resolve = colorResolver(el)
  const base = browserRgba(resolve('var(--dt-treemap-container-bg)'))
  const style: CellStyle = {
    bg: ground.bg && resolve(ground.bg),
    bands: ground.bands?.map(b => ({ ...b, color: resolve(b.color) })),
    segments: ground.segments?.map(s => ({ ...s, color: resolve(s.color) })),
  }
  // A translucent container color sits on the page; treat it as opaque, as the canvas renderer's base is.
  return dustInk(style, alpha, base ?? CONTAINER_BG, browserRgba)
}

export function DustHatch({ w, h, count, color, ground, alpha = 1, frame = false, clearLabels = false, baseLines = 3, ratio = 1.32 }: DustHatchProps) {
  const ref = useRef<HTMLCanvasElement>(null)
  // A consumer's style object is new each render; redraw only when its colors change.
  const groundKey = ground ? JSON.stringify([ground.bg, ground.bands, ground.segments]) : ''
  useLayoutEffect(() => {
    const cv = ref.current
    if (!cv || w <= 0 || h <= 0) return
    const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
    cv.width = Math.round(w * dpr)
    cv.height = Math.round(h * dpr)
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, w, h)
    const ink = color ?? resolvedDustInk(cv, ground, alpha)
    if (frame) drawDustFrame(ctx, 0, 0, w, h, ink)
    drawDust(ctx, 0, 0, w, h, count, ink, baseLines, ratio, clearLabels ? labelNotch(cv, w) : null)
  }, [w, h, count, color, groundKey, alpha, frame, clearLabels, baseLines, ratio]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <canvas
      ref={ref}
      width={Math.max(1, Math.round(w))}
      height={Math.max(1, Math.round(h))}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
    />
  )
}
