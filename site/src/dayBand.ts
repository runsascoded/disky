import type { DateRange } from './Treemap'

/** A window of days (inclusive, epoch days) to brush the map to: leaf cells whose
 * `axis` date (`d` written, `a` last read) falls outside it dim. Hovering the
 * date gradient sets one around the pointer; the age chart's bars set theirs. */
export interface DayBand { lo: number; hi: number; axis: 'd' | 'a' }

/** The gradient's brush: the day under the pointer ± 5% of the span. */
export function gradientBand(range: DateRange, t: number, axis: DayBand['axis']): DayBand {
  const span = range.max - range.min
  const c = Math.round(range.min + Math.min(1, Math.max(0, t)) * span)
  const half = Math.round(0.05 * span)
  return { lo: Math.max(range.min, c - half), hi: Math.min(range.max, c + half), axis }
}
