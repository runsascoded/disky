import { axisTicks } from './runChart'

export interface DurationAxis { ticks: number[]; top: number; cut: readonly [number, number] | null }
const niceTicks = (top: number) => axisTicks(top).map(v => Number(v.toPrecision(12)))

/** Run durations in hours, with phase boundaries protected from omission.
 * Break only a large empty interval: at least a 4× separation, spanning half the complete range. */
export function durationAxis(values: readonly number[], autoBreak = true, boundaries: readonly number[] = []): DurationAxis {
  const totals = values.filter(v => Number.isFinite(v) && v >= 0)
  const sorted = [...new Set([...totals, ...boundaries].filter(v => Number.isFinite(v) && v >= 0))].sort((a, b) => a - b)
  const max = sorted.at(-1) ?? 0
  const ticks = niceTicks(max)
  const linear = { ticks, top: ticks.at(-1)!, cut: null }
  if (!autoBreak) return linear
  let best: readonly [number, number] | null = null
  for (let i = 1; i < sorted.length; i++) {
    const a = sorted[i - 1], b = sorted[i]
    if (!(a > 0 && b >= 4 * a && b - a >= max / 2)) continue
    // A long phase shared by otherwise similar runs is not an outlier population.
    if (!totals.some(v => v > 0 && v <= a) || !totals.some(v => v >= b)) continue
    const lower = niceTicks(a)
    const lo = lower.at(-1)!
    const step = lower[1]
    const hi = Number(((Math.ceil(b / step) - 1) * step).toPrecision(12))
    if (hi - lo < max / 2) continue
    if (!best || hi - lo > best[1] - best[0]) best = [lo, hi]
  }
  if (!best) return linear
  const lower = niceTicks(best[0])
  const upper = niceTicks(max - best[1]).map(v => Number((v + best[1]).toPrecision(12)))
  return { ticks: [...lower, ...upper], top: upper.at(-1)!, cut: best }
}

/** Split a stacked bar at the omitted interval, preserving both visible pieces. */
export function durationSegments(from: number, to: number, cut: DurationAxis['cut']): [number, number][] {
  if (to <= from) return []
  if (!cut) return [[from, to]]
  return [[from, Math.min(to, cut[0])], [Math.max(from, cut[1]), to]]
    .filter(([a, b]) => b > a) as [number, number][]
}
