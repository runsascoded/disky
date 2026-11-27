/** The over-time chart's change strip (specs/change-metrics.md §4.2): the series' per-scan change flows summed per UTC
 *  day, or per ISO week over a long span, never per scan (a gcs scan every few hours isn't a dip). Pure. */
import { DAY } from './scan'

/** A series point's flows (`/api/series`, §4.1); a point without `added` wasn't counted. */
export interface FlowPoint { date: string; added?: number; deleted?: number; modified?: number; since?: string }

/** One bar: the bucket's start (UTC ms), its flows summed, `net` = added − deleted (the change in objects), the scans
 *  it sums (oldest first), and the earliest `since` among them (a scan the series lacks: its changes are in here). */
export interface ChangeBar { t: number; added: number; deleted: number; modified: number; net: number; scans: string[]; since?: string }

export type Bucket = 'day' | 'week'

/** Past this many days shown, a bar is a week. */
export const WEEK_PAST_DAYS = 90

/** The bucket for a span of `ms`. */
export const bucketFor = (ms: number): Bucket => (ms > WEEK_PAST_DAYS * DAY ? 'week' : 'day')

/** A scan id's UTC day (its `YYYY-MM-DD`), as ms. */
export const dayOfScan = (date: string): number => Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10)))

/** A UTC day's ISO week's Monday. */
export const mondayOf = (t: number): number => t - ((new Date(t).getUTCDay() + 6) % 7) * DAY

/** The counted points' flows per `bucket`, oldest first; a bucket with no counted scan has no bar (absent, not zero). */
export function changeBars(points: readonly FlowPoint[], bucket: Bucket): ChangeBar[] {
  const out = new Map<number, ChangeBar>()
  for (const p of [...points].sort((a, b) => a.date.localeCompare(b.date))) {
    if (p.added === undefined) continue
    const day = dayOfScan(p.date)
    const t = bucket === 'week' ? mondayOf(day) : day
    const b = out.get(t) ?? { t, added: 0, deleted: 0, modified: 0, net: 0, scans: [] }
    b.added += p.added
    b.deleted += p.deleted ?? 0
    b.modified += p.modified ?? 0
    b.net = b.added - b.deleted
    b.scans.push(p.date)
    if (p.since && (b.since === undefined || p.since < b.since)) b.since = p.since
    out.set(t, b)
  }
  return [...out.values()].sort((a, b) => a.t - b.t)
}

/** The strip's extent: the tallest bar above the zero line (written: added + modified) and below it (deleted). */
export function barExtent(bars: readonly ChangeBar[]): { up: number; down: number } {
  return { up: Math.max(0, ...bars.map(b => b.added + b.modified)), down: Math.max(0, ...bars.map(b => b.deleted)) }
}
