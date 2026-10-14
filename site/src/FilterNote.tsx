import type { ReactNode } from 'react'
import { Tooltip } from './Tooltip'

/** What a filtered response says about its own completeness (`/api/subtree`,
 * `/api/diff`): a read budget stopped the search (`partial`), or the matches
 * came from a thresholded read rather than the search index (`approximate`). */
export interface Coverage {
  partialReason?: string
  approximateReason?: string
  /** A heavy literal's fleet root from the static catalog alone (`rollup.bucketsOnly`): exact per-bucket
   *  totals, but no view inside a bucket without the full index — or, `'scoped'` (`^q`'s starts-with catalog,
   *  `rollup.scopedBelow`), a view inside one where the term's matches there are few enough. */
  bucketsOnly?: boolean | 'scoped'
  /** The scan lists folders only (a v1 index; the response's `dirsOnly`): its matches are folder names only. */
  dirsOnly?: boolean
  /** The static name index's hex-run rule, applied to this literal (specs/static-hex-runs.md): its matches
   * inside hex ids of `min`+ digits aren't counted. */
  hexRuns?: { min: number; tail: number }
}

/** The hex-run note's text: why a hex-looking literal may match less than a substring search would. */
export const hexRunsNote = (min: number): string => `Matches inside long hex IDs (${min}+ hex digits) aren't indexed.`

/** An info icon whose tooltip says the hex-run rule applied (`hexRunsNote`). */
export function HexRunsInfo({ min }: { min: number }) {
  const note = hexRunsNote(min)
  return (
    <Tooltip content={note}>
      <span className="fflag fhex" role="note" aria-label={note}>ⓘ</span>
    </Tooltip>
  )
}

/** The `bucketsOnly` flag's words. */
export const BUCKETS_ONLY = 'per-bucket totals only: searching inside a bucket for this term needs the full search index'
export const DIRS_ONLY = 'this scan lists folders only — files aren’t searchable on it'
export const BUCKETS_ONLY_SCOPED = 'per-bucket totals only at the top: inside a bucket, this term is searched where its matches are few enough'

/** The coverage flags, spelled out — never a silent "fewer matches". */
export function FilterFlags({ partialReason, approximateReason, bucketsOnly, dirsOnly, hexRuns }: Coverage) {
  return (
    <>
      {dirsOnly && <span className="fflag">{DIRS_ONLY}</span>}
      {bucketsOnly && <span className="fflag">{bucketsOnly === 'scoped' ? BUCKETS_ONLY_SCOPED : BUCKETS_ONLY}</span>}
      {partialReason && <span className="fflag">partial results: {partialReason}</span>}
      {approximateReason && <span className="fflag">approximate: {approximateReason}</span>}
      {hexRuns && <HexRunsInfo min={hexRuns.min} />}
    </>
  )
}

/** "N matched": the drilled folder's matched bytes (the view root's, not the fleet's), saying which —
 *  `in this folder` when drilled, `fleet-wide` at the root. */
export function matchedNote(b: number, drilled: boolean, fmt: (b: number) => string): string {
  if (b <= 0) return drilled ? 'no matches in this folder' : 'no matches'
  return `${fmt(b)} matched ${drilled ? 'in this folder' : 'fleet-wide'}`
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
