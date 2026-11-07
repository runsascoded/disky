// The numbers line of the map's cards (the resting view card and a hovered cell's): pure, so the wording is tested
// apart from the treemap.
import { epochDaysToDate } from './colors'
import { fmtInstant } from './scan'

/** A node's share of the view's total (`12.34% of total`); `null` when it *is* the total (the root, or a view equal
 *  to it), where "100.00% of total" says nothing. A share just under the whole that would round to 100.00 reads
 *  `>99.99%`. */
export function shareOfTotal(b: number, total: number): string | null {
  if (!total || b >= total) return null
  const pct = ((100 * b) / total).toFixed(2)
  return `${pct === '100.00' ? '>99.99' : pct}% of total`
}

/** A node's newest listing time (the newest object created — modified, on S3/R2 — at or under it): its instant
 *  with the time of day, as the scan picker labels a scan (`10/21 10:39a`), when the answer carries `t`; the
 *  date alone (`10/21`) from an answer cached before it did. */
export function newestLabel(n: { d?: number; t?: number }, now = new Date()): string | null {
  return n.t != null ? fmtInstant(n.t * 1000, now) : n.d != null ? epochDaysToDate(n.d, now) : null
}
