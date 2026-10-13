/** The size chart's slot when there is no chart to draw: its failure (a filter refusal's reason as is, muted, no
 *  retry — the same words as the filter box and the map; anything else red, with a retry), else the skeleton while
 *  the series loads, else that fewer than two scans hold the path. A refused page view never sends the series (it
 *  has no match roots to sum), so its refusal is passed in and stands for the series' own. */
import { Skeleton } from './Busy'
import { LoadFailure } from './LoadFailure'

export function SeriesEmpty({ err, loading, onRetry }: {
  /** The series' failure, or the page view's refusal. */
  err?: unknown
  loading: boolean
  onRetry?: () => void
}) {
  if (err) return <LoadFailure err={err} what="series" onRetry={onRetry} />
  return loading ? <Skeleton height={220} label="loading series…" /> : <p className="loading">fewer than two scans hold this path</p>
}
