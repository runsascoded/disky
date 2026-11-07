import { useLayoutEffect, useRef } from 'react'

/**
 * A canvas cross-hatch that stands in for a folded "(other)" tile — the long
 * tail of cells too small to draw as their own rects. Instead of one flat grey
 * blob, it draws horizontal + vertical rules whose spacing *tightens toward the
 * lower-right*, evoking the continued shrink of the dust it represents, and
 * whose count scales with how many real cells were folded in (denser hatch =
 * more hidden items). Styled to read as distinct from real cells: thin rules on
 * a faded ground, not a solid fill with a label.
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
  /** Rule color. Default: a translucent neutral that reads on either ground. */
  color?: string
  /**
   * Rules per axis at `count = 1`, before the count scaling. The axis with
   * more room gets proportionally more. Default: 3.
   */
  baseLines?: number
  /** Geometric growth of the gap moving away from the dense (lower-right)
   * corner. >1. Default: 1.32. */
  ratio?: number
}

/** Line offsets from the dense end (0 = the tightest gap), growing
 * geometrically, that sum to `len`. Returns `n` interior positions measured as
 * distance from the dense end. */
export function dustOffsets(len: number, n: number, ratio: number): number[] {
  if (n < 1 || len <= 0) return []
  // Σ g·ratio^k for k=0..n-1 = len  ⇒  g = len·(ratio−1)/(ratioⁿ−1).
  const g = ratio === 1 ? len / n : (len * (ratio - 1)) / (ratio ** n - 1)
  const out: number[] = []
  let at = 0
  for (let k = 0; k < n; k++) {
    at += g * ratio ** k
    if (at >= len) break
    out.push(at)
  }
  return out
}

/** Target rule count on an axis of length `len`, given how many cells folded
 * in — grows with `log2(count)`, clamped so a rule is never under ~4px apart. */
export function dustLineCount(len: number, count: number, base: number): number {
  const byCount = Math.round(base * Math.log2(count + 1))
  return Math.max(2, Math.min(byCount, Math.floor(len / 4)))
}

/**
 * Stroke the dust cross-hatch into an already-configured 2D context, at CSS-px
 * origin `(ox, oy)`. Self-contained (sets stroke style + width, does its own
 * `beginPath`/`stroke`), so both the standalone `<DustHatch>` canvas and the
 * whole-map canvas renderer draw an identical tile. `ctx` is assumed already
 * dpr-scaled by the caller; coordinates are CSS px.
 */
export function drawDust(
  ctx: CanvasRenderingContext2D,
  ox: number,
  oy: number,
  w: number,
  h: number,
  count: number,
  color = 'rgba(150, 150, 165, 0.5)',
  baseLines = 3,
  ratio = 1.32,
): void {
  if (w <= 0 || h <= 0) return
  ctx.strokeStyle = color
  ctx.lineWidth = 1
  ctx.beginPath()
  // Vertical rules: dense at the right edge (x = w), gaps grow leftward.
  for (const d of dustOffsets(w, dustLineCount(w, count, baseLines), ratio)) {
    const x = Math.round(w - d) + 0.5
    ctx.moveTo(ox + x, oy)
    ctx.lineTo(ox + x, oy + h)
  }
  // Horizontal rules: dense at the bottom edge (y = h), gaps grow upward.
  for (const d of dustOffsets(h, dustLineCount(h, count, baseLines), ratio)) {
    const y = Math.round(h - d) + 0.5
    ctx.moveTo(ox, oy + y)
    ctx.lineTo(ox + w, oy + y)
  }
  ctx.stroke()
}

export function DustHatch({ w, h, count, color, baseLines = 3, ratio = 1.32 }: DustHatchProps) {
  const ref = useRef<HTMLCanvasElement>(null)
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
    drawDust(ctx, 0, 0, w, h, count, color ?? 'rgba(150, 150, 165, 0.5)', baseLines, ratio)
  }, [w, h, count, color, baseLines, ratio])

  return (
    <canvas
      ref={ref}
      width={Math.max(1, Math.round(w))}
      height={Math.max(1, Math.round(h))}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
    />
  )
}
