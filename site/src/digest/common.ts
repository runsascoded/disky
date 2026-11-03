/** The digest renderer's shared pieces: config and params, arrow degrees, the `?d=` link tokens and the OP window
 * (`cloud/src/dt_cloud/digest.py`'s, ported byte for byte: `./parity.test.ts`). Pure; DOM-free. */
import { addDays, dayOf, fixed, grouped, md, MONTHS, pyRound } from './pyfmt'

export const MINUS = '−'

export interface Quota { bytes: number; name: string; short: string | null; classes: number[] }
export interface BucketCfg { label: string | null; path: string | null; zone: string | null; quotas: Quota[] }

/** The digest config's display fields, as the job writes them (`digest.cfg_json`). */
export interface DigestCfg {
  template: 'cw' | 'gcs'
  title: string
  site_url: string
  host: string
  unit: { name: string; bytes: number; short: string }
  primary: string | null
  buckets: Record<string, BucketCfg>
  prices: Record<string, number>
  icons_base: string
}

/** What the page can vary (specs/slack-preview-page.md); `DEFAULT_PARAMS` is what the job posts. */
export interface DigestParams {
  /** The window's length in days, inclusive of today. */
  windowDays: number
  /** The latest scan's position across the plot's width on the month's 1st and last day (linear between). */
  todayAt: [number, number]
  /** The "What changed" map's span: the window's, or month-to-date. */
  diff: 'window' | 'mtd'
  /** The headline's figures, in order. */
  headline: ('mtd' | 'window')[]
  /** Which weeks the summaries list: every one overlapping the window, or the month's. */
  weeks: 'window' | 'month'
  /** cw: a quota line under the headline: every pool most full first, marked by pressure
   *  (specs/digest-quota-pressure.md); every pool in the replies' order, marked by runway (specs/digest-runway-marks.md);
   *  or none. */
  quotas: 'off' | 'circles' | 'runway' | 'runway-hue'
  /** cw: the plot's chart: the primary's usage against its quota, or every pool's % of its quota. */
  plot: 'usage' | 'pools'
  /** The share (%) a pool is marked red at; orange from 10 points below. */
  alertAt: number
}

export const DEFAULT_PARAMS: DigestParams = {
  windowDays: 30, todayAt: [0.7, 1], diff: 'window', headline: ['mtd', 'window'], weeks: 'window', quotas: 'off', plot: 'usage', alertAt: 90,
}

/** What the OP renders around its rows: the hosted plot's URL (null: no image line) and the previous thread's. */
export interface OpLinks { plotUrl: string | null; prevUrl: string | null }

// Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
const THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]

/** The signed arrow degree for a percent change, time-normalized by `mult` (`digest.deg`). */
export function deg(pct: number, mult = 1): number {
  const a = Math.abs(pct) * mult
  let d = 0
  THRESH.forEach((t, i) => { if (a >= t) d = (i + 1) * 10 })
  d = Math.min(80, d)
  return pct < 0 ? -d : d
}

/** A signed size change: `+1.5`, `−0.3` (`digest._tb`). */
export const tb = (v: number): string => (v >= 0 ? `+${fixed(v, 1)}` : `${MINUS}${fixed(Math.abs(v), 1)}`)

/** A change's percent of its baseline, unsigned (`digest._pct`). */
export function pct(dtb: number, total: number): string {
  const prev = total - dtb
  return fixed(prev ? Math.abs((dtb / prev) * 100) : 0, 1)
}

/** `$` + an integer, comma-grouped, signed (`digest_gcs._usd`). */
export const usd = (v: number): string => (v >= 0 ? '+$' : `${MINUS}$`) + grouped(Math.abs(v), 0)

/** The `?d=` look-back token for a → b (epoch ms): `1d12h`, `7d`, `12h` (`digest._span`). */
export function span(a: number, b: number): string {
  const h = pyRound((b - a) / 3600_000)
  const [days, hours] = [Math.floor(h / 24), h % 24]
  return (days ? `${days}d` : '') + (hours ? `${hours}h` : '') || '0h'
}

/** The window's first day (epoch ms): the longer of `days` days to `today`, inclusive, and the month to date
 *  (`digest.window_start`). */
export const windowStart = (month: number, today: number, days: number): number => Math.min(month, addDays(today, -(days - 1)))

/** A day's ISO week's Monday (`digest.week_of`). */
export const weekOf = (t: number): number => addDays(t, -((new Date(t).getUTCDay() + 6) % 7))

/** The look-back figure's label (`digest.window_label`): `last 30d` from a scan before the window, else `since M/D`. */
export const windowLabel = (start: number, since: string, days: number): string =>
  dayOf(since) < start ? `last ${days}d` : `since ${md(dayOf(since))}`

/** The OP's link to the previous month's thread (`digest.prev_month_link`), as lines. */
export const prevMonthLink = (month: number, url: string | null): string[] =>
  url ? [`[← ${MONTHS[(new Date(month).getUTCMonth() + 11) % 12]}](${url})`] : []

/** A month id (`YYYY-MM`) as its 1st's UTC midnight. */
export const monthOf = (m: string): number => dayOf(`${m}-01`)
