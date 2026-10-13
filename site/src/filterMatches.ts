// What a filtered subtree response hands the size-over-time series: its match roots, and their count.
export interface MatchFields {
  /** Match roots: every one, or — `matchesCapped` — at most the server's `MATCH_LIST_CAP` of them. */
  matched?: { path: string; b: number; o: number }[]
  /** Every match root's count and totals, whether or not `matched` lists them all. */
  matchCount?: { n: number; b: number; o: number }
  matchesCapped?: true
}

/** A capped list is a subset, never a root list to sum per scan: the series gets only the exact count, so
 *  it asks with the query (`q=`) instead of `paths=`. */
export function seriesMatches(d: MatchFields | undefined): { paths?: string[]; pathsTotal?: number } {
  return { paths: d?.matchesCapped ? undefined : d?.matched?.map(x => x.path), pathsTotal: d?.matchCount?.n }
}
