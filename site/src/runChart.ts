import type { Param } from 'use-prms'
import type { HistoryPoint } from './runHistory'

const { ceil, floor, log10, max, min, pow, round } = Math
export interface RunWindow { end: number | null; duration: number | null }
const DEFAULT_WINDOW: RunWindow = { end: null, duration: 3600 }

export function parseDuration(text: string): number | null {
  if (!/^(?:\d+(?:\.\d+)?[dhms])+$/.test(text)) return null
  let seconds = 0
  for (const match of text.matchAll(/(\d+(?:\.\d+)?)([dhms])/g)) seconds += Number(match[1]) * ({ d: 86400, h: 3600, m: 60, s: 1 }[match[2]] ?? 0)
  return Number.isFinite(seconds) && seconds >= 60 && seconds <= 365 * 86400 ? round(seconds) : null
}

export function durationLabel(seconds: number): string {
  return seconds % 86400 === 0 ? `${seconds / 86400}d` : seconds % 3600 === 0 ? `${seconds / 3600}h` : seconds % 60 === 0 ? `${seconds / 60}m` : `${round(seconds)}s`
}

/** Awair's end + lookback model, using explicit UTC for shared links. */
export const runWindowParam: Param<RunWindow> = {
  encode: ({ end, duration }) => {
    if (end === null && duration === 3600) return undefined
    const width = duration === null ? 'all' : durationLabel(duration)
    return end === null ? width : `${new Date(end * 1000).toISOString()}~${width}`
  },
  decode: encoded => {
    if (!encoded) return DEFAULT_WINDOW
    const parts = encoded.split('~')
    if (parts.length > 2) return DEFAULT_WINDOW
    const width = parts.at(-1)!
    const duration = width === 'all' ? null : parseDuration(width)
    if (width !== 'all' && duration === null) return DEFAULT_WINDOW
    const end = parts.length === 2 ? Date.parse(parts[0]) / 1000 : null
    return end !== null && !Number.isFinite(end) ? DEFAULT_WINDOW : { end, duration }
  },
}

export function chartRange(points: readonly HistoryPoint[], window: RunWindow, latest: number): [number, number] {
  const end = window.end ?? latest
  const start = window.duration === null ? min(points[0]?.ts ?? end - 3600, end - 60) : end - window.duration
  return [start, end]
}

export function shiftWindow(range: [number, number], direction: -1 | 1): RunWindow {
  const duration = range[1] - range[0]
  return { end: round(range[1] + direction * duration / 2), duration: round(duration) }
}

export function zoomWindow(range: [number, number], factor: number): RunWindow {
  const duration = max(60, min(365 * 86400, round((range[1] - range[0]) * factor)))
  return { end: round((range[0] + range[1]) / 2 + duration / 2), duration }
}

/** Time-weighted trailing rate, never bridges missing samples. */
export function rollingRates(points: readonly HistoryPoint[], seconds: number, metric: 'rate' | 'byteRate' = 'rate'): (number | null)[] {
  return points.map((point, i) => {
    if (point[metric] === null) return null
    if (!seconds) return point[metric]
    let weight = 0, sum = 0
    for (let j = i; j > 0; j--) {
      const right = points[j], left = points[j - 1]
      const rate = right[metric]
      if (rate === null || right.ts - left.ts > 180 || right.ts <= left.ts) break
      const overlap = right.ts - max(left.ts, point.ts - seconds)
      if (overlap <= 0) break
      weight += overlap
      sum += overlap * rate
    }
    return weight ? sum / weight : null
  })
}

/** Zero-anchored specialization of pltly's alignDualAxes: choose a shared
 * interval count minimizing relative padding, with nice steps on both axes.
 * Work in display units (Ti/Gi for bytes), then rescale to source units. */
export function alignedTicks(top1: number, top2: number): [number[], number[]] {
  const nice = (v: number) => {
    const base = pow(10, floor(log10(v)))
    return ([1, 2, 2.5, 5, 10].find(n => n * base >= v * (1 - 1e-10)) ?? 10) * base
  }
  const a = max(top1, 1e-9), b = max(top2, 1e-9)
  let score = Infinity, best: [number[], number[]] = [axisTicks(a), axisTicks(b)]
  for (let n = 3; n <= 5; n++) {
    const s1 = nice(a / n), s2 = nice(b / n)
    const padding = n * s1 / a + n * s2 / b - 2
    if (padding < score) { score = padding; best = [Array.from({ length: n + 1 }, (_, i) => i * s1), Array.from({ length: n + 1 }, (_, i) => i * s2)] }
  }
  return best
}

export function axisTicks(top: number): number[] {
  if (!(top > 0)) return [0, 0.25, 0.5, 0.75, 1]
  const magnitude = pow(10, floor(log10(top / 4)))
  const normalized = top / 4 / magnitude
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude
  const count = ceil(top / step)
  return Array.from({ length: count + 1 }, (_, i) => i * step)
}

export function percentOf(value: number, planned: number): string {
  if (!(planned > 0)) return '—'
  const percent = 100 * value / planned
  if (value > 0 && percent < 0.05) return '<0.1%'
  if (value < planned && percent >= 99.95) return '>99.9%'
  return `${percent.toFixed(1)}%`
}
