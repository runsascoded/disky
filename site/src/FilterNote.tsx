import type { ReactNode } from 'react'

/** What a filtered response says about its own completeness (`/api/subtree`,
 * `/api/diff`): a read budget stopped the search (`partial`), or the matches
 * came from a thresholded read rather than the search index (`approximate`). */
export interface Coverage {
  partialReason?: string
  approximateReason?: string
}

/** The coverage flags, spelled out — never a silent "fewer matches". */
export function FilterFlags({ partialReason, approximateReason }: Coverage) {
  return (
    <>
      {partialReason && <span className="fflag">partial results: {partialReason}</span>}
      {approximateReason && <span className="fflag">approximate: {approximateReason}</span>}
    </>
  )
}

/** The note beside the filter box: the query's error, else what matched and
 * how completely; `children` is the clear button. */
export function FilterNote({ error, matched, coverage, children }: {
  error?: string
  /** "12 GiB matched" / "no matches" (null: nothing loaded yet). */
  matched: string | null
  coverage?: Coverage
  children?: ReactNode
}) {
  if (!error && matched == null) return null
  return (
    <span className={`fnote${error ? ' ferr' : ''}`} role={error ? 'alert' : undefined}>
      {error ?? <>{matched}{coverage && <FilterFlags {...coverage} />}</>}
      {children}
    </span>
  )
}
