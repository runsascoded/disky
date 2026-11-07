import { Link } from 'react-router-dom'
import { fmtScan, type ScanLabel, type ScanMiss } from './scan'
import { encodeSel, exactPrefix, minPrefix, type ScanSel, type ScanTimes } from './scanSlug'

/** The URL a "no scan matches" link goes to: the same page and params, its
 * `?d=` re-pointed at `scan` — the end (`end: true`) or the pinned start —
 * keeping the rest of the selection; the legacy keys dropped. `scan` is written
 * as its canonical slug among `scans` (`minPrefix`), or its minute without them. */
export function hrefWithScan(pathname: string, search: string, sel: ScanSel | undefined, scan: string, end = true, times?: ScanTimes, scans?: readonly string[]): string {
  const sp = new URLSearchParams(search)
  sp.delete('date'); sp.delete('from')
  const base: ScanSel = sel && !sel.invalid ? sel : {}
  const exact = scans ? minPrefix(scan, scans, times) : exactPrefix(scan, times)
  const d = encodeSel(end ? { ...base, d: exact } : { ...base, from: exact, span: undefined })
  if (d) sp.set('d', d); else sp.delete('d')
  return `${pathname}?${sp}`
}

/** A selection that names no scan: say so, and offer the closest scans on
 * either side — never show a different scan in its place. */
export function NoScanMatch({ miss, hrefFor, what = 'scan', fmt = fmtScan }: { miss: ScanMiss; hrefFor: (scan: string) => string; what?: string; fmt?: ScanLabel }) {
  const { slug, before, after } = miss
  return (
    <p className="no-scan loading" role="alert">
      No {what} matches <code>{slug}</code>.
      {before || after ? <>
        {' '}Nearest:{' '}
        {before && <Link to={hrefFor(before)}>← {fmt(before)}</Link>}
        {before && after && ' · '}
        {after && <Link to={hrefFor(after)}>{fmt(after)} →</Link>}
      </> : ' No scan to offer instead.'}
    </p>
  )
}
