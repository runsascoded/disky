import { useEffect, useMemo } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { useUrlAlias, useUrlState, useUrlStates } from 'use-prms'
import { encodeCutoff, parseCutoff } from './federatedScans'
import { canonicalizeSel, decodeSel, encodeScan, encodeSel, exactSlug, latestScan, minPrefix, minSlug, legacyDateParam, legacyFromParam, mergeSel, scanCmp, scanInstant, scanMatches, scanNeighbors, scanParts, selSlug, sortScans, startKey, timesNeeded, type ScanSel, type ScanTimes } from './scanSlug'
import { storeUrl, type Store } from './stores'
import { pickScanSelection } from './scanSelection'
import { floatingScan } from './defaultScan'

// How often an unpinned tab re-checks for newly published scans.
export const SCANS_POLL_MS = 5 * 60_000

/** Capture/publication details use the viewer's timezone, labelled explicitly. */
export function fmtTimestamp(at: string, timeZone?: string): string {
  return new Date(at).toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone,
  })
}


// Scan labels: drop the redundant year for the current one, so a list of
// same-year scans reads as `8/17` rather than `2026-08-17`. Scan ids are
// `YYYY-MM-DD`, or timed as `YYYY-MM-DDTHHMM`.
export function fmtScan(s: string, now = new Date()): string {
  const p = scanParts(s)
  if (!p) return s
  const { y, mo, d, hh, mm } = p
  if (!hh) {
    // Date-only ids are calendar dates, not instants —
    // rendering them through a timezone would shift some readers a day off.
    return Number(y) === now.getFullYear() ? `${Number(mo)}/${Number(d)}` : `${y}-${mo}-${d}`
  }
  // Timed ids are UTC instants; display in the viewer's local time,
  // 12-hour with a bare a/p ("8/19 6:08a"). The `?d=` token stays UTC (see
  // decodeScan) — display converts, the URL doesn't.
  return fmtInstant(Date.UTC(+y, +mo - 1, +d, +hh, +(mm ?? '0')), now)
}

/** An instant (epoch ms) as the scan picker labels a timed scan: the viewer's local time, 12-hour with a bare a/p
 *  (`10/21 10:39a`), the year only when it isn't the current one (`2025-10-21 10:39a`). */
export function fmtInstant(ms: number, now = new Date()): string {
  const dt = new Date(ms)
  const h = dt.getHours()
  const time = `${h % 12 || 12}:${String(dt.getMinutes()).padStart(2, '0')}${h < 12 ? 'a' : 'p'}`
  const md = `${dt.getMonth() + 1}/${dt.getDate()}`
  return dt.getFullYear() === now.getFullYear()
    ? `${md} ${time}`
    : `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${time}`
}

/** A scan's label, knowing its peers: a timed id, or a date-only id whose
 * start is known (`times`), reads as its local instant ("10/9 12:30a"); a
 * date-only id sharing its day with no known start says so ("10/9 (time
 * unknown)"); one alone on its day with no known start is its date ("10/8").
 * `scans`: the whole list (any order), not a filtered view of it. */
export type ScanLabel = (id: string) => string
export function scanLabeler(scans: readonly string[], times: ScanTimes = {}, now = new Date()): ScanLabel {
  const shared = new Set(timesNeeded(scans))
  return id => times[id] ? fmtScan(times[id], now) : shared.has(id) ? `${fmtScan(id, now)} (time unknown)` : fmtScan(id, now)
}

export { DAY, decodeScan, decodeSel, decodeSpan, encodeScan, encodeSel, encodeSpan, latestScan, nearestScan, resolveScan, scanInstant, scanMatches, scanNeighbors, scanTime, type ScanSel, type ScanTimes } from './scanSlug'

const selParam = { encode: encodeSel, decode: (e: string | undefined) => decodeSel(e) }

/** `?at=` / `?atf=`: a union view's cutoff instants — the page's end, the diff's start — when no one scan id says
 *  them (`pickCutoff`, specs/federated-scans.md "Case 1 status"). Compact UTC in the URL (`20261013T1935Z`,
 *  `encodeCutoff`; ISO reads too), the canonical ISO instant in the page; an unparseable value reads as absent. */
export const cutoffParam = {
  encode: (v: string | undefined) => (v ? encodeCutoff(v) : undefined),
  decode: (e: string | undefined) => parseCutoff(e) ?? undefined,
}

/** The page's scan at a cutoff instant: the newest of `scans` at or before it (a date-only id at its known
 *  start, else its midnight). Null: none by then. */
export function scanAtInstant(scans: readonly string[], at: string, times?: ScanTimes): string | null {
  const t = Date.parse(at)
  return sortScans(scans, times).find(s => scanInstant(s, times) <= t) ?? null
}

/** The page's scan selection (`?d=`), read through use-prms's alias hook so a
 * legacy `?date=<id>` / `?from=<id>` (or an ISO `?d=2026-10-06`) folds into
 * the one canonical `?d=` on mount (specs/done/scan-ids-not-dates.md §3). Writes go
 * through `useUrlState` so a scan pick is a history entry (back button). */
export function useScanSel(): [ScanSel | undefined, (v: ScanSel | undefined) => void] {
  const [sel] = useUrlAlias<ScanSel>({
    keys: ['d', 'date', 'from'],
    params: { d: selParam, date: legacyDateParam(), from: legacyFromParam() },
    merge: mergeSel,
  })
  const [, setSel] = useUrlState('d', selParam, true)
  return [sel, setSel]
}

/** Rewrite the URL's `?d=` in place (no history entry) to its canonical form
 * once the scan list has answered: each pinned endpoint that names one scan for
 * good becomes that scan's shortest slug (`canonicalizeSel`) — an old minute
 * link `2610081236` reads `261008` once 10/8 is over and held no other scan. */
function useCanonicalSel(sel: ScanSel | undefined, scans: readonly string[], loaded: boolean, times: ScanTimes) {
  const [, replaceSel] = useUrlState('d', selParam, false)
  const enc = encodeSel(sel)
  const canon = loaded ? encodeSel(canonicalizeSel(sel, scans, times)) : enc
  useEffect(() => {
    if (canon !== enc) replaceSel(decodeSel(canon))
  }, [canon, enc]) // eslint-disable-line react-hooks/exhaustive-deps
}

/** A scan id → its canonical `?d=` slug among the store's scans (`minSlug`),
 * for links built outside a page's own selection (the scan-runs pages); the
 * minute form (`exactSlug`) until the list answers. */
export function useMinSlug(store: Store): (id: string) => string {
  const scansQ = useScans(store)
  const listed = useMemo(() => scansQ.data ?? [], [scansQ.data])
  const times = useScanTimes(store, listed)
  return useMemo(() => (scansQ.isSuccess ? (id: string) => minSlug(id, listed, times) : (id: string) => exactSlug(id, times)), [scansQ.isSuccess, listed, times])
}

/** A `?d=` slug that matches no scan: the slug as written (canonical when it
 * decoded), and the closest scans on either side to offer instead. */
export interface ScanMiss { slug: string; before: string | null; after: string | null }

/** The selection's miss, once the scan list has answered: an unparseable
 * value, or an end slug no scan matches. (A pinned start that misses is the
 * diff's own state — see `fromMiss`.) */
export function scanMiss(sel: ScanSel | undefined, scans: readonly string[], loaded: boolean, times?: ScanTimes): ScanMiss | null {
  if (!loaded || !sel) return null
  if (sel.invalid) return { slug: sel.invalid, before: null, after: null }
  if (!sel.d || latestScan(sel.d, scans, times)) return null
  return { slug: selSlug(sel), ...scanNeighbors(sel.d, scans, times) }
}

/** A pinned start (`from`) that matches no scan before `after`. */
export function fromMiss(from: string | undefined, after: string | null, scans: readonly string[], times?: ScanTimes): ScanMiss | null {
  if (!from || !after) return null
  const earlier = scans.filter(s => scanCmp(s, after, times) < 0)
  return latestScan(from, earlier, times) ? null : { slug: encodeScan(from) ?? from, ...scanNeighbors(from, earlier, times) }
}

export interface Scan {
  asof: string | null
  /** `?d=` names no scan (then `asof` is null): what to say and offer. */
  miss: ScanMiss | null
  /** With no `?d=` and `indexed` preferred: the newer scans skipped (not yet indexed), newest first. */
  pending: string[]
  /** Newest first, by time (a date-only scan at its start, when known). */
  scans: string[]
  /** The known starts of the date-only scans (`useScanTimes`). */
  times: ScanTimes
  /** Labels a scan among its peers (`scanLabeler`). */
  label: ScanLabel
  dMatches: string[]
  dP: string | undefined
  /** Pick the "after" scan, preserving an existing pinned-end mode. */
  setDP: (v: string | undefined) => void
  /** Diff look-back in ms; undefined = the previous scan (or a pinned `from`). */
  span: number | undefined
  setSpan: (ms: number | undefined) => void
  /** "Before" pinned to a scan (decoded id); undefined = use `span`. Setting it
   *  clears `span` (the two "before" forms are mutually exclusive). */
  from: string | undefined
  setFrom: (v: string | undefined) => void
  /** Pin the "after" endpoint at the current scan, or release it to follow the
   *  latest scan. (Only meaningful while the page is on the latest scan; an
   *  older `asof` is already pinned.) */
  setEndPin: (pin: boolean) => void
  /** Pin + look-back in one URL write (a chart brush sets both). */
  setRange: (d: string | undefined, ms: number | undefined, before?: string) => void
  /** `?at=`: the end as a cutoff instant (a union view's pick no one scan id says); `asof` is then the newest
   *  scan at or before it (`scanAtInstant`). Any other end pick clears it. */
  at: string | undefined
  setAt: (at: string) => void
  /** `?atf=`: the diff's start as a cutoff instant; a start scan pin or span clears it. */
  atFrom: string | undefined
  setFromAt: (at: string) => void
  scansQ: UseQueryResult<string[]>
}

// Shared scan resolution: `?d=YYMMDD` (a prefix of a scan id) pins a scan;
// absent means "latest" (a first-class state, so a parked tab follows new scans
// via the poll rather than freezing on the day it opened). Every scan-scoped
// page (home map, /users, /user/:id) uses this so a scan pin is one shareable,
// page-independent dimension. `scans` is newest-first, so the first prefix match
// is the newest. See specs/done/scan-param-all-pages.md.
/** The store's scan list (newest first) — the one definition every page
 * shares, so the poll and error handling are the same wherever it mounts.
 * The list polls so an unpinned tab discovers new scans on its own; the
 * per-scan payloads are immutable once published, so they never refetch.
 * Store-scoped key, so switching stores swaps the whole payload set. */
export function useScans(store: Store): UseQueryResult<string[]> {
  return useQuery<string[]>({
    queryKey: ['scans', store.key],
    // Throw on non-OK (e.g. a 401 from the data proxy with no session) so
    // react-query holds it as an error rather than handing the error body
    // downstream — `scans` then stays `[]` and the error surfaces as a sign-in
    // prompt rather than crashing `scans.map`.
    queryFn: async () => {
      const r = await fetch(storeUrl(`${store.base}/scans.json`, store))
      if (!r.ok) throw Object.assign(new Error(`scans: ${r.status}`), { status: r.status })
      return r.json()
    },
    refetchInterval: SCANS_POLL_MS,
  })
}

/** The scan list has answered and named nothing. The data Function lists a
 * snapshot only once its index rows are in D1 (`index_schema`), so an empty
 * list means no indexed snapshot exists for this store — the page has nothing
 * to fetch, and should say so rather than leave the map's skeleton up. A
 * pending or failed list is not "empty": the skeleton and the error strip
 * cover those. */
export const noScansYet = (q: { isSuccess: boolean; data?: string[] }): boolean =>
  q.isSuccess && q.data?.length === 0

export { floatingScan } from './defaultScan'

/** The note a floating view shows when it skipped newer scans: filtered, "Searching 10/9 4:30a, the newest
 * indexed scan; 10/9 8:36a is still being indexed."; unfiltered (a `DEFAULT_SCAN=searchable` deployment's default),
 * "Showing 10/9 4:30a, the newest searchable scan; …". (`pending` newest first.) */
export function pendingNote(scan: string, pending: readonly string[], now = new Date(), filtered = true): string {
  const names = [...pending].reverse().map(s => fmtScan(s, now))
  const list = names.length <= 2 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  const lead = filtered ? `Searching ${fmtScan(scan, now)}, the newest indexed scan` : `Showing ${fmtScan(scan, now)}, the newest searchable scan`
  return `${lead}; ${list} ${names.length === 1 ? 'is' : 'are'} still being indexed.`
}

/** The scan a page shows: an explicit `?d=` resolves to the latest scan it names (a miss — none, or an
 * unparseable value — is `miss`, never another scan); absent, `floatingScan` (the newest scan, or with
 * `indexed` the newest covered one, `pending` the newer ones skipped; `defaultScan.ts` says when). */
export function selectScan(sel: ScanSel | undefined, scans: readonly string[], loaded: boolean, indexed?: readonly string[] | null | 'loading', times?: ScanTimes): { asof: string | null; miss: ScanMiss | null; pending: string[] } {
  const miss = scanMiss(sel, scans, loaded, times)
  if (miss || sel?.invalid) return { asof: null, miss, pending: [] }
  if (sel?.d) return { asof: latestScan(sel.d, scans, times), miss: null, pending: [] }
  const f = floatingScan(scans, indexed)
  return { asof: f.scan, miss: null, pending: f.pending }
}

/** The known start minutes of the store's date-only scans, in one request:
 * the store's `scan-times.json` (each scan's `path` pointer's
 * `index_schema.started`), keyed by `startKey`. A scan with no recorded start
 * is left out: its midnight form stands. */
export function useScanTimes(store: Store, scans: readonly string[]): ScanTimes {
  const q = useQuery<Record<string, string>>({
    queryKey: ['scan-times', store.key],
    queryFn: async () => {
      const r = await fetch(storeUrl(`${store.base}/scan-times.json`, store))
      if (!r.ok) throw new Error(`scan-times: ${r.status}`)
      return r.json()
    },
    enabled: scans.some(id => id.length === 10),
    refetchInterval: SCANS_POLL_MS,
  })
  return useMemo(() => {
    const out: Record<string, string> = {}
    for (const id of scans) {
      const k = startKey(id, q.data?.[id])
      if (k) out[id] = k
    }
    return out
  }, [q.data, scans])
}

/** The page's scan selection. `indexed`: the covered scans to prefer when `?d=` is absent (`defaultScan.ts`
 * `prefersIndexed`: a filtered view, or every view on a `DEFAULT_SCAN=searchable` deployment). An explicit `?d=`
 * resolves as always. */
export function useScan(store: Store, indexed?: readonly string[] | null | 'loading'): Scan {
  const [sel] = useScanSel()
  // One writer for the end/start selection and the cutoff instants, so a pick is one history entry.
  const { values: cuts, setValues } = useUrlStates({ d: selParam, at: cutoffParam, atf: cutoffParam }, true)
  const { at, atf: atFrom } = cuts
  const scansQ = useScans(store)
  const listed = useMemo(() => scansQ.data ?? [], [scansQ.data])
  const times = useScanTimes(store, listed)
  const scans = useMemo(() => sortScans(listed, times), [listed, times])
  const label = useMemo(() => scanLabeler(scans, times), [scans, times])
  const floating = useMemo(() => floatingScan(scans, indexed), [scans, indexed])
  // `?at=` names the end exactly; the page's scan is the newest at or before it.
  const picked = useMemo(
    () => (at ? { asof: scansQ.isSuccess ? scanAtInstant(scans, at, times) : null, miss: null, pending: [] } : selectScan(sel, scans, scansQ.isSuccess, indexed, times)),
    [at, sel, scans, scansQ.isSuccess, indexed, times],
  )
  const dP = sel?.d
  const span = sel?.span
  const from = sel?.from
  const dMatches = useMemo(() => scanMatches(dP, scans, times), [dP, scans, times])
  const { asof, miss, pending } = picked
  // Write the {end, before} pair verbatim; start pins and spans are exclusive (with each other and with `atf`),
  // as an end slug is with `at`.
  const write = (d: string | undefined, span0: number | undefined, from0: string | undefined, at0: string | undefined, atf0: string | undefined) =>
    setValues({
      d: d || span0 || from0
        ? { ...(d ? { d } : {}), ...(span0 ? { span: span0 } : {}), ...(from0 ? { from: from0 } : {}) }
        : undefined,
      at: at0,
      atf: atf0,
    })
  useCanonicalSel(sel, scans, scansQ.isSuccess, times)
  // Setters take scan ids (a picker's choice) and write each as its canonical
  // prefix (`minPrefix`): the shortest of its day, hour and minute that names it
  // for good — never a day or hour that holds (or may yet hold) another scan.
  const exact = (v: string | undefined) => (v ? minPrefix(v, scans, times) : undefined)
  const pick = (v: string | undefined, ms: number | undefined, before?: string) => {
    const next = pickScanSelection(sel, v, floating.scan, ms, before)
    write(next?.d === dP ? dP : exact(next?.d), next?.span, next?.from === from ? from : exact(next?.from), undefined, next?.from || next?.span ? undefined : atFrom)
  }
  const setRange = (v: string | undefined, ms: number | undefined, before?: string) => pick(v, ms, before)
  const setDP = (v: string | undefined) => pick(v, span)
  const setSpan = (ms: number | undefined) => write(dP, ms, undefined, at, undefined)
  const setFrom = (v: string | undefined) => write(dP, undefined, exact(v), at, undefined)
  const setEndPin = (pin: boolean) => write(pin ? exact(asof ?? undefined) : undefined, span, from, undefined, atFrom)
  const setAt = (t: string) => write(undefined, span, from, t, atFrom)
  const setFromAt = (t: string) => write(dP, undefined, undefined, at, t)
  return { asof, miss, pending, scans, times, label, dMatches, dP, setDP, span, setSpan, from, setFrom, setEndPin, setRange, at, setAt, atFrom, setFromAt, scansQ }
}

/** `<optgroup>` rows for a scan picker: scans grouped by their displayed
 * day (the label's date part, viewer-local for timed ids), in the given order
 * (newest first), each option labelled by its time alone (`8:01a`) — a
 * date-only scan alone on its day is that day's single, unlabelled-time entry.
 * A list of sixty `9/16 8:01p` rows read as noise; grouped, the day is said
 * once. `label`: the page's labeler (`scanLabeler`), default `fmtScan`. */
export function scanGroups(scans: string[], now = new Date(), label: ScanLabel = id => fmtScan(id, now)): { day: string; scans: { id: string; label: string }[] }[] {
  const out: { day: string; scans: { id: string; label: string }[] }[] = []
  for (const id of scans) {
    const f = label(id)
    const sp = f.indexOf(' ')
    const day = sp < 0 ? f : f.slice(0, sp)
    const lbl = sp < 0 ? f : f.slice(sp + 1)
    const last = out[out.length - 1]
    if (last && last.day === day) last.scans.push({ id, label: lbl })
    else out.push({ day, scans: [{ id, label: lbl }] })
  }
  return out
}
