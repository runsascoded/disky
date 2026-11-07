import { type DateAxis, dayAt } from './dateScale'

/** A window of days (inclusive, epoch days) to brush the map to: leaf cells whose
 * `axis` date (`d` written, `a` last read) falls outside it dim. Hovering the
 * date gradient sets one around the pointer; the age chart's bars set theirs. */
export interface DayBand { lo: number; hi: number; axis: 'd' | 'a' }

/** The gradient's brush: the days under the pointer ± 5% of the gradient's width (in the axis's
 * scale, so the window is what the eye sees, not a fixed span of days). */
export function gradientBand(axis: DateAxis, t: number, which: DayBand['axis']): DayBand {
  t = Math.min(1, Math.max(0, t))
  return { lo: dayAt(axis, t - 0.05), hi: dayAt(axis, t + 0.05), axis: which }
}
