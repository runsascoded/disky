import { enumParam, useUrlState } from 'use-prms'
import type { DateRange } from './Treemap'
import { dateColor } from './colors'

const { expm1, log1p, max, min, round } = Math

/** How a day maps onto the date gradient (`?ts=`, shared by the map and the tables):
 * - `log` (default): log of age back from the newest day in view, so recent weeks spread out and
 *   the old tail compresses;
 * - `lin`: linear in calendar time;
 * - `rank`: by the bytes in view (a byte-weighted CDF), so each stretch of the gradient covers
 *   about the same area; two close hues can be far apart in time. */
export type DateScale = 'log' | 'lin' | 'rank'
export const DATE_SCALES: readonly DateScale[] = ['log', 'lin', 'rank']

export const useDateScale = () => useUrlState('ts', enumParam<DateScale>('log', DATE_SCALES))

/** A day range with its scale: `t` maps a day onto [0, 1] (old → new), `day` inverts it (the
 * gradient key's brush and readout). */
export interface DateAxis extends DateRange {
  scale: DateScale
  t: (d: number) => number
  day: (t: number) => number
}

const clamp01 = (t: number) => min(1, max(0, t))

/** Piecewise-linear interpolation over increasing knots `xs` → `ys`, clamped to the ends. */
function interp(xs: number[], ys: number[], x: number): number {
  if (x <= xs[0]) return ys[0]
  const n = xs.length
  if (x >= xs[n - 1]) return ys[n - 1]
  let lo = 0
  let hi = n - 1
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1
    if (xs[m] <= x) lo = m
    else hi = m
  }
  return ys[lo] + ((ys[hi] - ys[lo]) * (x - xs[lo])) / (xs[hi] - xs[lo])
}

/** The axis over `range`. `rank` needs `samples` (`[day, bytes]` per cell in view); it falls back to
 * `lin` with fewer than two distinct days among them. */
export function dateAxis(range: DateRange, scale: DateScale, samples: readonly (readonly [number, number])[] = []): DateAxis {
  const { min: lo, max: hi } = range
  const span = hi - lo
  if (scale === 'log') {
    const L = log1p(span)
    return { ...range, scale, t: d => clamp01(1 - log1p(max(0, hi - d)) / L), day: t => hi - expm1((1 - clamp01(t)) * L) }
  }
  if (scale === 'rank') {
    // Each distinct day's knot sits at the middle of its bytes in the CDF, rescaled so the oldest
    // and newest days land on the gradient's ends. A zero-byte cell still counts (weight 1).
    const w = new Map<number, number>()
    for (const [d, b] of samples) if (d >= lo && d <= hi) w.set(d, (w.get(d) ?? 0) + max(1, b))
    const xs = [...w.keys()].sort((a, b) => a - b)
    if (xs.length >= 2) {
      const total = xs.reduce((s, d) => s + w.get(d)!, 0)
      let below = 0
      const mids = xs.map(d => {
        const m = below + w.get(d)! / 2
        below += w.get(d)!
        return m / total
      })
      const m0 = mids[0]
      const ys = mids.map(m => (m - m0) / (mids[mids.length - 1] - m0))
      return { ...range, scale, t: d => interp(xs, ys, d), day: t => interp(ys, xs, clamp01(t)) }
    }
  }
  return { ...range, scale: scale === 'rank' ? 'lin' : scale, t: d => clamp01((d - lo) / span), day: t => lo + clamp01(t) * span }
}

/** A table's date ink: the axis over its own rows' days (bytes as the `rank` weights). */
export function dateInk(samples: readonly (readonly [number, number])[], scale: DateScale): (d: number) => string {
  if (!samples.length) return () => dateColor(1)
  let lo = Infinity
  let hi = -Infinity
  for (const [d] of samples) {
    lo = min(lo, d)
    hi = max(hi, d)
  }
  if (!(hi > lo)) return () => dateColor(1)
  const axis = dateAxis({ min: lo, max: hi }, scale, samples)
  return d => dateColor(axis.t(d))
}

/** Round a day from the axis (the brush's window, the readout). */
export const dayAt = (axis: DateAxis, t: number) => round(axis.day(t))
