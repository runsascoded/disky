/**
 * The dust cross-hatch's geometry and ink, DOM-free: shared by the canvas
 * renderer (`TreemapCanvas`), the DOM `<DustHatch>` and the SVG export
 * (`svg.ts`), so every renderer draws the same rules in the same ink.
 */
import { CONTAINER_BG, parseColor } from './colors'
import type { CellStyle } from './cellTypes'

export type RGBA = [number, number, number, number]

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

/** The hatch's rules in a `w`×`h` box, as pixel-snapped (`n + 0.5`) offsets from
 * its origin: `xs` vertical rules, dense at the right edge; `ys` horizontal,
 * dense at the bottom. */
export function dustLines(w: number, h: number, count: number, baseLines = 3, ratio = 1.32): { xs: number[]; ys: number[] } {
  if (w <= 0 || h <= 0) return { xs: [], ys: [] }
  return {
    xs: dustOffsets(w, dustLineCount(w, count, baseLines), ratio).map(d => Math.round(w - d) + 0.5),
    ys: dustOffsets(h, dustLineCount(h, count, baseLines), ratio).map(d => Math.round(h - d) + 0.5),
  }
}

/** A clear pocket at the top-left of a hatch box (its label's extent): no rule crosses it. */
export interface DustNotch { w: number; h: number }

/** The hatch's rules in a `w`×`h` box: verticals as `[x, y0]` (running from `y0` to `h`), horizontals as `[y, x0]`
 *  (from `x0` to `w`), at `dustLines`' offsets. A rule that would cross `notch` (the label) starts past it instead,
 *  and one wholly inside it is dropped, so no rule strikes through the label while the rest of the box keeps its
 *  full density. */
export function dustRules(
  w: number,
  h: number,
  count: number,
  notch?: DustNotch | null,
  baseLines = 3,
  ratio = 1.32,
): { v: [number, number][]; h: [number, number][] } {
  const { xs, ys } = dustLines(w, h, count, baseLines, ratio)
  const nw = notch ? Math.max(0, notch.w) : 0
  const nh = notch ? Math.max(0, notch.h) : 0
  return {
    v: xs.map((x): [number, number] => [x, x < nw ? Math.ceil(nh) : 0]).filter(([, y0]) => y0 < h),
    h: ys.map((y): [number, number] => [y, y < nh ? Math.ceil(nw) : 0]).filter(([, x0]) => x0 < w),
  }
}

/** Hatch ink over a light ground. */
export const HATCH_DARK = 'rgba(0, 0, 0, 0.6)'
/** Hatch ink over a dark ground. */
export const HATCH_LIGHT = 'rgba(255, 255, 255, 0.6)'
const DARK_A = 0.6
const LIGHT_A = 0.6

const lum = (c: readonly number[]) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

/**
 * The hatch's ink: black or white, whichever stands out more from the ground
 * it's drawn on. The ground is `fill` painted at `alpha` over the opaque
 * `base` (the renderers' cell base); a null `fill` (unparseable) leaves the
 * base. Each ink's contrast is its alpha times its luminance distance from the
 * ground; the larger wins.
 */
export function hatchInk(fill: RGBA | null, alpha: number, base: readonly number[] = CONTAINER_BG): string {
  const a = fill ? Math.max(0, Math.min(1, fill[3] * alpha)) : 0
  const ground = (fill ? lum(fill) * a : 0) + lum(base) * (1 - a)
  return ground * DARK_A >= (255 - ground) * LIGHT_A ? HATCH_DARK : HATCH_LIGHT
}

/** The color a hatch sits on: the largest band, else the largest makeup segment, else `bg`. */
export function dustGround(style: CellStyle): string | undefined {
  const largest = (xs?: { color: string; frac: number }[]) =>
    xs?.reduce<{ color: string; frac: number } | undefined>((a, b) => !a || b.frac > a.frac ? b : a, undefined)?.color
  return largest(style.bands) ?? (style.segments && style.segments.length > 1 ? largest(style.segments) : undefined) ?? style.bg
}

/** Hatch ink for a cell `style` painted at `alpha` over `base`. `parse` turns a
 * (var-resolved) CSS color into RGBA; in a browser, pass one that also handles
 * what `parseColor` can't (`color-mix()`, named colors). */
export function dustInk(
  style: CellStyle,
  alpha = 1,
  base: readonly number[] = CONTAINER_BG,
  parse: (c: string) => RGBA | null = parseColor,
): string {
  const ground = dustGround(style)
  return hatchInk(ground ? parse(ground) : null, alpha, base)
}
