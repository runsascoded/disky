/** Python's number and date formatting, exactly, for the digest renderer (`./cw.ts`, `./gcs.ts`), whose text must
 * match the job's byte for byte (specs/slack-preview-page.md). JS `toFixed` rounds an exact tie away from zero;
 * Python's `format` and `round` round it to even (`f"{712.5:.0f}"` is `712`). Pure; DOM-free. */

/** Python's `f"{x:.{d}f}"`. */
export function fixed(x: number, d: number): string {
  const neg = x < 0 || Object.is(x, -0)
  const a = Math.abs(x)
  // a double's exact decimal expansion: under 100 fractional digits at any magnitude the digest formats
  const [int, frac] = a.toFixed(100).split('.')
  let s = a.toFixed(d)
  if (/^50*$/.test(frac.slice(d))) {
    const kept = d ? `${int}.${frac.slice(0, d)}` : int
    // a tie: round half to even, i.e. keep the truncation when its last digit is even
    if (Number(kept[kept.length - 1]) % 2 === 0) s = kept
  }
  return neg ? `-${s}` : s
}

/** Python's `f"{x:,.{d}f}"`: `fixed` with the integer part's thousands comma-grouped. */
export function grouped(x: number, d: number): string {
  const s = fixed(x, d)
  const sign = s.startsWith('-') ? '-' : ''
  const [int, frac] = s.slice(sign.length).split('.')
  return sign + int.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (frac === undefined ? '' : `.${frac}`)
}

/** Python's `round(x, d)` on a float (`round(x)`: `d` 0). */
export function pyRound(x: number, d = 0): number {
  return Number(fixed(x, d))
}

export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

const DAY = 86400_000

/** A `YYYY-MM-DD` date as its UTC midnight, epoch ms. */
export const dayOf = (s: string): number => Date.UTC(Number(s.slice(0, 4)), Number(s.slice(5, 7)) - 1, Number(s.slice(8, 10)))

/** An epoch-ms instant's UTC date, `YYYY-MM-DD` (Python's `date.isoformat()`). */
export const iso = (t: number): string => new Date(t).toISOString().slice(0, 10)

/** `n` days after day `t` (epoch ms). */
export const addDays = (t: number, n: number): number => t + n * DAY

/** Python's `f"{d:%b}"` (`Oct`). */
export const mon3 = (t: number): string => MONTHS[new Date(t).getUTCMonth()].slice(0, 3)

/** Python's `f"{d:%B %Y}"` (`October 2026`). */
export const monthYear = (t: number): string => `${MONTHS[new Date(t).getUTCMonth()]} ${new Date(t).getUTCFullYear()}`

/** A date's `M/D` (the digest's `_md`). */
export const md = (t: number): string => `${new Date(t).getUTCMonth() + 1}/${new Date(t).getUTCDate()}`

/** Python's `f"{t:%H:%M}"` (UTC). */
export const hhmm = (t: number): string => new Date(t).toISOString().slice(11, 16)
