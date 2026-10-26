/** A scan the static name index hasn't appended yet (published, not yet searchable; `defaultScan.ts` `searchOff`):
 *  why search is off for it, and the two ways on — the scan's indexing run on /scans, and the same view at the newest
 *  searchable scan. The search box's tip (`SearchOffTip`) and the map slot's note for a `?f=` the index refused
 *  (`SearchOffNote`) say the same. */
import { Link } from 'react-router-dom'
import type { ScanLabel } from './scan'
import { scansHref, useScanRunOf } from './scanRunsUi'
import type { Store } from './stores'

export interface CatchUpProps {
  store: Store
  /** The page's scan, which the index doesn't cover yet. */
  scan: string
  /** The newest scan the index covers, and this view's link to it (`?d=` set to it); null: none covered. */
  newest: string | null
  newestHref: string | null
  fmt: ScanLabel
}

/** Why search is off: the static name index appends a scan after it publishes. */
export const CATCH_UP = 'Search catches up when the static name index appends this scan.'

/** The scan's run on /scans (its indexing job; the run list until the runs load or when none recorded the scan), and
 *  the newest searchable scan in this view. */
export function CatchUpLinks({ store, scan, newest, newestHref, fmt }: CatchUpProps) {
  const run = useScanRunOf(store, scan)
  return (
    <>
      <Link to={scansHref(store, run?.run_id)}>{run ? 'its indexing job' : 'scan runs'}</Link>
      {newest && newestHref && <>{' · '}<Link to={newestHref}>newest searchable scan ({fmt(newest)})</Link></>}
    </>
  )
}

/** The disabled search box's tip. */
export function SearchOffTip(props: CatchUpProps) {
  return (
    <div className="search-off-tip">
      <p>Search isn’t available for {props.fmt(props.scan)} yet. {CATCH_UP}</p>
      <p><CatchUpLinks {...props} /></p>
    </div>
  )
}

/** The map slot for a `?f=` view on such a scan (the server's `scan-not-indexed` refusal), in place of an error. */
export function SearchOffNote(props: CatchUpProps) {
  return (
    <p className="loading filter-refused search-off" role="status">
      Search isn’t available for {props.fmt(props.scan)} yet. {CATCH_UP}{' '}
      <CatchUpLinks {...props} />
    </p>
  )
}
