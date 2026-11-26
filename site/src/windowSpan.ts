/** A diff window's length and the rates over it (Ryan, 2026-11-26): the window's duration stated beside its pickers,
 *  and each movement row's Δ as a rate in a unit fitted to the window, with every unit in a tooltip. */

const { abs, floor, round } = Math

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR
/** A mean Gregorian month and year: a rate's denominator, not a calendar span. */
const MONTH = 30.436875 * DAY
const YEAR = 365.2425 * DAY

export type RateUnit = 'h' | 'day' | 'mo' | 'yr'
export const RATE_UNITS: [RateUnit, number][] = [['h', HOUR], ['day', DAY], ['mo', MONTH], ['yr', YEAR]]

/** The window's length in its two largest units, rounded at the second: `45m`, `3h 20m`, `5d 18h`, `6d`. */
export function fmtSpan(ms: number): string {
  const m = round(ms / MINUTE)
  if (m < 60) return `${m}m`
  if (m < 48 * 60) return m % 60 ? `${floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m / 60}h`
  const h = round(ms / HOUR)
  return h % 24 ? `${floor(h / 24)}d ${h % 24}h` : `${h / 24}d`
}

/** `n` to `digits` places, then its unit, singular when it reads as exactly 1. */
const count = (n: number, digits: number, unit: string) => {
  const text = n.toLocaleString('en-US', { maximumFractionDigits: digits })
  return `${text} ${unit}${text === '1' ? '' : 's'}`
}

/** The same length in other units, for the duration's tooltip: `138 hours`, `8,279 minutes`, `5.75 days`, `0.82 weeks`. */
export function spanUnits(ms: number): string[] {
  return [
    count(ms / HOUR, 1, 'hour'),
    count(round(ms / MINUTE), 0, 'minute'),
    count(ms / DAY, 2, 'day'),
    count(ms / (7 * DAY), 2, 'week'),
  ]
}

/** The rate unit fitted to a window: the largest of h, day, mo, yr no longer than it (an hour for anything shorter). */
export function rateUnit(ms: number): RateUnit {
  let unit: RateUnit = 'h'
  for (const [u, len] of RATE_UNITS) if (len <= ms) unit = u
  return unit
}

/** `delta` over `ms` per `unit`, signed as the row's Δ is (`fmtD`): `+22.4 T/day`, `−1.2K/h`. */
export function fmtRate(delta: number, ms: number, unit: RateUnit, fmtD: (d: number) => string): string {
  const len = RATE_UNITS.find(([u]) => u === unit)![1]
  const per = (delta * len) / ms
  return `${fmtD(abs(per) < 1e-9 ? 0 : per)}/${unit}`
}
