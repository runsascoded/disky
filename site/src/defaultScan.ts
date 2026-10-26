/** The scan a view with no `?d=` opens on: the one resolver the map, its table and the link cards share
 *  (specs/done/search-gated-default-scan.md). React-free, so the Functions (`_lib/og/data.ts`) call it too.
 *
 *  A deployment picks its default with the site var `DEFAULT_SCAN` (`/api/filter-caps` hands it to the client):
 *  - `newest` (unset): the newest published scan. On one the static name index hasn't appended yet, the search
 *    box is shown disabled, with links to the indexing run and the newest searchable scan.
 *  - `searchable`: the newest scan the static name index covers; a newer published scan is reachable by `?d=`.
 *
 *  Either way a filtered view (`?f=`) with no `?d=` opens on the newest covered scan: its query can only be answered
 *  there. Without the static filter (no covered list) the default is the newest scan. */

export type DefaultScanMode = 'newest' | 'searchable'

/** `DEFAULT_SCAN`'s value: unset or blank is `newest`; anything but the two modes is a config error. */
export function defaultScanMode(v: string | undefined): DefaultScanMode {
  const s = v?.trim()
  if (!s) return 'newest'
  if (s === 'newest' || s === 'searchable') return s
  throw new Error(`DEFAULT_SCAN=${s}: want newest | searchable`)
}

/** Whether a view with no `?d=` prefers the newest covered scan: every view on a `searchable` deployment, a
 *  filtered one on any. */
export const prefersIndexed = (mode: DefaultScanMode, filtered: boolean): boolean => mode === 'searchable' || filtered

/** The newest scan, or — `indexed` given (the static index's covered scans) — the newest one it covers, with
 *  `pending` the newer scans it doesn't cover yet (published, not yet appended). None covered: the newest scan
 *  (whose search then says it isn't indexed). `'loading'`: the covered list is on its way, so nothing is picked
 *  yet (no fetch of a scan it may rule out). `scans` is newest first. */
export function floatingScan(scans: readonly string[], indexed?: readonly string[] | null | 'loading'): { scan: string | null; pending: string[] } {
  if (indexed === 'loading') return { scan: null, pending: [] }
  if (!indexed) return { scan: scans[0] ?? null, pending: [] }
  const have = new Set(indexed)
  const i = scans.findIndex(s => have.has(s))
  return i < 0 ? { scan: scans[0] ?? null, pending: [] } : { scan: scans[i], pending: scans.slice(0, i) }
}

/** The default scan of a view: `floatingScan` over the covered list when the view prefers it (`prefersIndexed`),
 *  else the newest scan. `scans` newest first; `indexed` the covered scans (null: no static filter). */
export function defaultScan(scans: readonly string[], indexed: readonly string[] | null | 'loading', mode: DefaultScanMode, filtered: boolean): { scan: string | null; pending: string[] } {
  return floatingScan(scans, prefersIndexed(mode, filtered) ? indexed : null)
}

/** The scans newer than the newest covered one: published, not yet appended to the static name index (what a
 *  picker marks "indexing"). Empty without the static filter, or when nothing is covered. `scans` newest first. */
export const indexingScans = (scans: readonly string[], indexed: readonly string[] | null | 'loading'): string[] =>
  indexed === 'loading' || !indexed ? [] : floatingScan(scans, indexed).pending

/** Whether the search box is off for `scan`: the static filter is on (a covered list) and doesn't cover it. Unknown
 *  (no scan, the list loading): on. */
export const searchOff = (scan: string | null | undefined, indexed: readonly string[] | null | 'loading'): boolean =>
  !!scan && Array.isArray(indexed) && !indexed.includes(scan)
