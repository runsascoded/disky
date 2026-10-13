/** Scan ids, scan slugs and the `?d=` selection codec — the one definition the
 * client pages and the Functions share (DOM-free, React-free).
 *
 * A scan's identity is its **scan id**: date-only `YYYY-MM-DD`, or timed
 * `YYYY-MM-DDTHHMM` (UTC). A date is never a scan key — a
 * day may hold several scans (specs/scan-ids-not-dates.md).
 *
 * A **slug** is a user-facing spelling of a scan-id *prefix*; it resolves to
 * the **latest** indexed scan whose id starts with it (`resolveScan`):
 *
 *   261009       · 2026-10-09        → the latest `2026-10-09…` scan
 *   26100912     · 2026-10-09T12     → the latest `2026-10-09T12…` scan
 *   2610091200   · 2026-10-09T1200   → exactly that scan
 *
 * A slug matching no scan is a miss — never the nearest or the latest scan
 * instead: the APIs answer 404, the pages say so (`scanNeighbors` names the
 * closest scans on either side to offer).
 *
 * The canonical (URL) form is the dashless compact one, `YYMMDD[HH[MM]]`
 * (`encodeScan`), which leaves `-` to `?d=`'s separators alone; a pinned scan
 * is written as its shortest permanent slug (`minSlug`).
 */

const HOUR = 3600_000
export const DAY = 24 * HOUR

/** A scan id's shape (`isScanId` also checks the calendar). */
export const SCAN_ID_RE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$/

/** A real calendar date, and (if present) a real UTC time of day. */
function validParts(y: string, mo: string, d: string, hh?: string, mm?: string): boolean {
  const t = new Date(Date.UTC(+y, +mo - 1, +d))
  return t.getUTCFullYear() === +y && t.getUTCMonth() === +mo - 1 && t.getUTCDate() === +d
    && (hh === undefined || +hh <= 23) && (mm === undefined || +mm <= 59)
}

/** A full scan id: `YYYY-MM-DD` or `YYYY-MM-DDTHHMM`, calendar-valid. What the
 * APIs accept (they take exact ids; slugs resolve client-side). */
export function isScanId(v: unknown): v is string {
  if (typeof v !== 'string') return false
  const m = SCAN_ID_RE.exec(v)
  return !!m && m[1] !== '0000' && validParts(m[1], m[2], m[3], m[4], m[5])
}

/** A scan id's (or id-led string's) parts, leniently: `YYYY-MM-DD`, then an
 * optional `T`/space-separated `HH[:]MM` — the one parser the formatters and
 * `scanTime` share. Null when it doesn't lead with a date. */
export function scanParts(s: string): { y: string; mo: string; d: string; hh?: string; mm?: string } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):?(\d{2})?)?/.exec(s)
  return m ? { y: m[1], mo: m[2], d: m[3], ...(m[4] ? { hh: m[4] } : {}), ...(m[5] ? { mm: m[5] } : {}) } : null
}

/** A scan id's instant (UTC). Date-only ids read as midnight UTC. */
export const scanTime = (d: string): number => {
  const p = scanParts(d)
  return p ? Date.UTC(+p.y, +p.mo - 1, +p.d, +(p.hh ?? '0'), +(p.mm ?? '0')) : NaN
}

/** A scan-run id: `<scan id>-p<plan>/<YYYYMMDDTHHMMSSZ>` (a sweep run's dir). */
export const RUN_ID_RE = new RegExp(`^${SCAN_ID_RE.source.slice(1, -1)}-p\\d+\\/\\d{8}T\\d{6}Z$`)

/** Compact (canonical URL) form of a scan id or id prefix — `YYMMDD[HH[MM]]`,
 * dashless: `2026-10-09T1236` → `2610091236`, `2026-10-09T12` → `26100912`,
 * `2026-10-09` → `261009`. Anything else passes through. */
export const encodeScan = (v: string | undefined): string | undefined => {
  const m = v && /^\d{2}(\d{2})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(v)
  if (!m) return v || undefined
  const [, y, mo, d, hh, mm] = m
  return `${y}${mo}${d}${hh ?? ''}${mm ?? ''}`
}

/** A slug → the scan-id prefix it names (ISO form), or undefined if it isn't
 * one. Spellings (all UTC):
 *   261009 · 26100912 · 2610091200                 (YYMMDD[HH[MM]]; canonical)
 *   261009-12 · 261009-1200 · 261009T12            (compact with a separator; legacy)
 *   2026-10-09 · 2026-10-09T12 · 2026-10-09T1200   (ISO; legacy)
 *   10-9-12 · 10-9-12-00                           (M-D-H[-MM], current year)
 *   26-10-9-12 · 2026-10-9-12                      (year-first when the lead
 *                                                   component can't be a month) */
export const decodeScan = (e: string | undefined, now = new Date()): string | undefined => {
  if (!e) return undefined
  const pad = (n: string) => n.padStart(2, '0')
  const out = (y: string, mo: string, d: string, hh?: string, mm?: string) =>
    validParts(y, mo, d, hh, mm) ? `${y}-${mo}-${d}` + (hh ? `T${hh}${mm ?? ''}` : '') : undefined
  // 6, 8 or 10 digits (an 8-digit run is YYMMDDHH, never YYYYMMDD), or the
  // legacy `-`/`T` before the time.
  const compact = /^(\d{2})(\d{2})(\d{2})(?:[T-]?(\d{2})(\d{2})?)?$/.exec(e)
  if (compact) {
    const [, y, mo, d, hh, mm] = compact
    return out(`20${y}`, mo, d, hh, mm)
  }
  const iso = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(e)
  if (iso) {
    const [, y, mo, d, hh, mm] = iso
    return out(y, mo, d, hh, mm)
  }
  const parts = e.split(/[T-]/)
  if (parts.length < 2 || parts.length > 5 || parts.some(p => !/^(\d{1,2}|\d{4})$/.test(p))) return undefined
  // A lead component that can't be a month is a year (2- or 4-digit); 4-digit
  // components anywhere else are malformed.
  const yearFirst = parts[0].length === 4 || Number(parts[0]) > 12
  if (parts.slice(yearFirst ? 1 : 0).some(p => p.length > 2)) return undefined
  const y = yearFirst ? (parts[0].length === 4 ? parts[0] : `20${parts[0]}`) : String(now.getUTCFullYear())
  const [mo, d, hh, mm] = parts.slice(yearFirst ? 1 : 0)
  if (!mo || !d) return undefined
  return out(y, pad(mo), pad(d), hh && pad(hh), mm && pad(mm))
}

// ---- a date-only scan's time: its real start, else its midnight ----
//
// A date-only id (`2026-10-09`) names one scan; the day slug `261009` names the
// day's *latest* scan, which may be a timed one (`2026-10-09T1236`). So a
// date-only scan matches, sorts and is linked by an effective minute, its
// **key** (`scanKey`):
//  - its real start, when known (`ScanTimes`: the earliest bucket listing's
//    `started`, which `path-index` writes to `meta.started`) — 2026-10-09's
//    04:30Z run is `2026-10-09T0430`, slug `2610090430`, and the hour slug
//    `26100904` takes it too;
//  - else its midnight, `2026-10-09T0000` (slug `2610090000`), the instant
//    `scanTime` gives an untimed id.
// The midnight stays an alias even when the start is known, so a midnight link
// already shared keeps resolving to it. Neither can collide with a real timed
// scan: a date and its `T0000` are one instant (`check_order` refuses the
// pair), and a start is that scan's own minute. No new syntax: every picker
// writes `exactSlug(id, times)`, which resolves to exactly that scan.

/** Date-only scan ids → their start minute (`YYYY-MM-DDTHHMM`, UTC; `startKey`).
 * Only the ids whose start is known are present; only date-only ids on a day
 * with more than one scan need one (`timesNeeded`). */
export type ScanTimes = Readonly<Record<string, string>>

/** A date-only scan's start key from its ISO `started` instant: the UTC minute,
 * when it falls on the id's own day (else null — a start on another day can't
 * key that day's scan; the midnight form stands). */
export function startKey(id: string, started: unknown): string | null {
  if (id.length !== 10 || !isScanId(id) || typeof started !== 'string') return null
  const t = new Date(started)
  if (isNaN(t.getTime())) return null
  const iso = t.toISOString()
  return iso.slice(0, 10) === id ? `${id}T${iso.slice(11, 13)}${iso.slice(14, 16)}` : null
}

/** The date-only scans whose start matters: those sharing their day with
 * another scan. A single-scan day's `261009` is already unique. */
export function timesNeeded(scans: readonly string[]): string[] {
  const perDay = new Map<string, number>()
  for (const s of scans) perDay.set(s.slice(0, 10), (perDay.get(s.slice(0, 10)) ?? 0) + 1)
  return scans.filter(s => s.length === 10 && (perDay.get(s) ?? 0) > 1)
}

/** A scan id's key: a timed id is itself; a date-only id its start
 * (`times`), else its midnight `T0000`. */
export const scanKey = (id: string, times?: ScanTimes): string => (id.length === 10 ? times?.[id] ?? `${id}T0000` : id)

/** Keys compare chronologically as strings; a tie (a start on a timed scan's
 * minute) breaks by id. Negative = `a` is earlier. */
export const scanCmp = (a: string, b: string, times?: ScanTimes): number => {
  const ka = scanKey(a, times), kb = scanKey(b, times)
  return ka < kb ? -1 : ka > kb ? 1 : a < b ? -1 : a > b ? 1 : 0
}

/** `scans` newest first, by key. */
export const sortScans = (scans: readonly string[], times?: ScanTimes): string[] => [...scans].sort((a, b) => scanCmp(b, a, times))

/** Whether `scan` falls under the decoded `prefix`: by id, by key, or (a
 * date-only scan) by its midnight alias. */
export const scanUnder = (scan: string, prefix: string, times?: ScanTimes): boolean =>
  scan.startsWith(prefix) || scanKey(scan, times).startsWith(prefix) || (scan.length === 10 && `${scan}T0000`.startsWith(prefix))

/** The decoded prefix that selects exactly the scan `id` (`2026-10-09` →
 * its start `2026-10-09T0430`, else `2026-10-09T0000`; a timed id is itself)
 * — what a picker writes as `d`. */
export const exactPrefix = (id: string, times?: ScanTimes): string => (isScanId(id) ? scanKey(id, times) : id)

/** A scan id's exact slug, resolving to exactly that scan: `2026-10-09T1236` →
 * `2610091236`, date-only `2026-10-09` → its start `2610090430` (or midnight
 * `2610090000` when unknown) — never `261009`, the day's latest scan. */
export const exactSlug = (id: string, times?: ScanTimes): string => encodeScan(exactPrefix(id, times))!

// ---- the canonical slug: the shortest that names its scan for good ----
//
// A scan's canonical slug is the shortest of its day, its hour and its minute
// (all of its key, `scanKey`) that names only that scan **and never can name
// another** (`namesForGood`):
//  - the day (`261008`) once that UTC day has ended, if no other scan shares it;
//  - else the hour (`26100904`) once that hour has ended, if no other scan
//    falls under it;
//  - else the minute (`2610091236`): always permanent (`check_order` refuses two
//    scans in one minute).
// So today's scan links by its minute (or hour) and its slug shortens once the
// period ends; the longer forms keep resolving to it, so a link already shared
// never breaks. Resolution is unchanged (a day slug is that day's latest scan).
// A date-only scan whose start isn't known (`ScanTimes`) could later be keyed
// into any hour of its day, so no slug on that day stops at the hour.
//
// "Ended" is the period's UTC end: a scan is listed once indexed, and its id is
// its start, so one started before the end but indexed after it could still
// join a period whose slug already shortened (an in-flight scan). The cadences
// in use (gcs daily, cw 6-hourly) never put two scans in one hour, and rarely in
// one day across midnight.

const MINUTE = 60_000

/** The UTC end of the period a decoded prefix names: its day, hour or minute. */
const periodEnd = (prefix: string): number =>
  scanTime(prefix) + (prefix.length === 10 ? DAY : prefix.length === 13 ? HOUR : MINUTE)

/** Whether the decoded `prefix` names the scan `id` alone among `scans`, and
 * always will: a minute always; a day or an hour once it has ended (`now`), and
 * an hour only on a day whose date-only scan (if any) has a known start. */
export function namesForGood(prefix: string, id: string, scans: readonly string[], times?: ScanTimes, now = new Date()): boolean {
  const under = scans.filter(s => scanUnder(s, prefix, times))
  if (under.length !== 1 || under[0] !== id) return false
  if (prefix.length >= 15) return true
  if (now.getTime() < periodEnd(prefix)) return false
  const day = prefix.slice(0, 10)
  return prefix.length === 10 || !scans.some(s => s === day && !times?.[s])
}

/** The scan `id`'s canonical prefix (decoded form): the shortest of its key's
 * day, hour and minute that `namesForGood`; the minute (`exactPrefix`) when
 * `id` isn't among `scans`. What a picker writes as `d`. */
export function minPrefix(id: string, scans: readonly string[], times?: ScanTimes, now = new Date()): string {
  const k = exactPrefix(id, times)
  for (const p of [k.slice(0, 10), k.slice(0, 13)]) if (namesForGood(p, id, scans, times, now)) return p
  return k
}

/** The scan `id`'s canonical slug (`minPrefix`, encoded): `261008` (alone on a
 * past day), `26100904` (alone in a past hour), else `2610091236`. */
export const minSlug = (id: string, scans: readonly string[], times?: ScanTimes, now = new Date()): string =>
  encodeScan(minPrefix(id, scans, times, now))!

/** A decoded prefix in canonical form: when it names one scan for good
 * (`namesForGood`), that scan's `minPrefix` (never a longer one); else as is —
 * a day still in progress, or one holding several scans, stays the day's
 * latest scan. */
export function canonicalPrefix(prefix: string, scans: readonly string[], times?: ScanTimes, now = new Date()): string {
  const id = latestScan(prefix, scans, times)
  if (!id || !namesForGood(prefix, id, scans, times, now)) return prefix
  const m = minPrefix(id, scans, times, now)
  return m.length <= prefix.length ? m : prefix
}

/** A selection with each pinned endpoint in canonical form (`canonicalPrefix`). */
export function canonicalizeSel(s: ScanSel | undefined, scans: readonly string[], times?: ScanTimes, now = new Date()): ScanSel | undefined {
  if (!s || s.invalid) return s
  const c = (p: string | undefined) => (p ? canonicalPrefix(p, scans, times, now) : undefined)
  return sel(c(s.d), s.span, c(s.from))
}

/** Every scan matching a decoded prefix, newest first (`scans` in any order). */
export function scanMatches(prefix: string | undefined, scans: readonly string[], times?: ScanTimes): string[] {
  if (!prefix) return []
  return sortScans(scans.filter(s => scanUnder(s, prefix, times)), times)
}

/** The latest of `scans` (any order) by key; null when empty. */
export function newestScan(scans: readonly string[], times?: ScanTimes): string | null {
  let best: string | null = null
  for (const s of scans) if (best === null || scanCmp(s, best, times) > 0) best = s
  return best
}

/** **The resolver**: the latest scan among `scans` (any order) under the
 * decoded prefix, by key — null when none is. */
export function latestScan(prefix: string | undefined, scans: readonly string[], times?: ScanTimes): string | null {
  if (!prefix) return null
  return newestScan(scans.filter(s => scanUnder(s, prefix, times)), times)
}

/** A slug (any spelling `decodeScan` reads) → the latest matching scan, or
 * null if the slug is malformed or matches nothing. */
export const resolveScan = (slug: string | undefined, scans: readonly string[], now = new Date(), times?: ScanTimes): string | null =>
  latestScan(decodeScan(slug, now), scans, times)

/** A missed prefix's closest scans: the latest scan before everything it
 * names, and the earliest after — the links a "no scan matches" state offers.
 * (Compared by `scanKey`; `prefix + '~'` sorts after every key it names.) */
export function scanNeighbors(prefix: string, scans: readonly string[], times?: ScanTimes): { before: string | null; after: string | null } {
  let before: string | null = null, after: string | null = null
  const end = `${prefix}~`
  for (const s of scans) {
    const k = scanKey(s, times)
    if (k < prefix && (before === null || scanCmp(s, before, times) > 0)) before = s
    if (k > end && (after === null || scanCmp(s, after, times) < 0)) after = s
  }
  return { before, after }
}

/** A scan's instant (UTC), by key: a date-only id at its start, else midnight. */
export const scanInstant = (id: string, times?: ScanTimes): number => scanTime(scanKey(id, times))

/** The scan nearest to `t` among `scans` (any order); null when empty. For a
 * look-back *span* (a duration, not a slug) and explicit UI picks (a chart
 * brush) only — a slug never snaps to its nearest scan. */
export const nearestScan = (scans: readonly string[], t: number, times?: ScanTimes): string | null => {
  let best: string | null = null
  for (const s of scans) if (!best || Math.abs(scanInstant(s, times) - t) < Math.abs(scanInstant(best, times) - t)) best = s
  return best
}

// ---- the `?d=` selection ----
//
// `?d=[end][-before]` — `end` is the "after" scan slug (absent = latest, a
// sticky state that follows new scans); `before` is the "before" endpoint,
// expressed *either* as a look-back span *or* as a second pinned scan:
//   ?d=-7d                       latest end, 7 days back (a floating window)
//   ?d=2609040002                end pinned to the 9/4 00:02Z scan, default look-back
//   ?d=2609040002-7d             end pinned, 7 days back
//   ?d=-2609010002               latest end, START pinned to 9/1 00:02Z
//   ?d=2609040002-2609010002     both endpoints pinned (a frozen window)
//   ?d=26090412-260901           …each endpoint any slug: a day, an hour, a minute
// Canonical slugs are dashless (`encodeScan`), so `-` only ever separates the
// end, the start and the span. Spans are `Nd`, `Nh`, or both (`6d12h`); a
// span resolves to the *nearest* scan (a duration, not a slug). Span and
// `from` are mutually exclusive.
//
// Decoding (`decodeSel`) also reads every legacy spelling. In order:
//  1. A value leading with a 4-digit year (`2026-10-09`, `2026-10-09T1200`) is
//     one ISO slug, whole (ISO never carried a range).
//  2. A trailing `-<span>` (`-7d`, `-12h`, `-6d12h`) is the look-back.
//  3. The rest splits on `-` into tokens. A leading empty token (`-261001…`)
//     means the end floats. Each 6-, 8- or 10-digit token (or `YYMMDDT…`)
//     starts a slug; a 2- or 4-digit token right after a *6-digit* token is that
//     day's legacy `-HH` / `-HHMM` time (`261009-12` = 10/9's noon hour). No
//     canonical slug is 2 or 4 digits, so this never collides with a range:
//     `261009-12` is one slug (an hour), `261009-261001` two (a range),
//     `261009-1200-261001-0600` two legacy minutes.
//  4. One slug = the end (or, after a leading `-`, the start); two = end and
//     start. Anything else (`26100912-12`, three slugs, a range plus a span)
//     is not a selection — unless the whole head is a year-less spelling
//     `decodeScan` reads (`10-9`, `8-19-10`).
// A value that decodes to nothing is kept verbatim as `invalid`: it selects no
// scan (a miss, shown as such), never the latest.

export const encodeSpan = (ms: number): string => {
  const days = Math.floor(ms / DAY)
  const hours = Math.round((ms - days * DAY) / HOUR)
  return (days ? `${days}d` : '') + (hours ? `${hours}h` : '') || '0h'
}

export const decodeSpan = (s: string): number | undefined => {
  const m = /^(?:(\d+)d)?(?:(\d+)h)?$/.exec(s)
  if (!m || !s) return undefined
  const ms = (+(m[1] ?? 0)) * DAY + (+(m[2] ?? 0)) * HOUR
  return ms > 0 ? ms : undefined
}

export interface ScanSel {
  /** "After" scan-id prefix (decoded form, e.g. `2026-09-04T0002`); absent = latest. */
  d?: string
  /** "Before" as a look-back in ms; absent = the previous scan. Excludes `from`. */
  span?: number
  /** "Before" as a pinned scan-id prefix (decoded form). Excludes `span`. */
  from?: string
  /** A `?d=` (or legacy key) value that names no selection, verbatim: it
   *  resolves to no scan, and re-encodes as itself. Excludes the rest. */
  invalid?: string
}

const SPAN_SUFFIX = /-(\d+d(?:\d+h)?|\d+h)$/
const SLUG_TOKEN = /^\d{6}(?:T\d{2}(?:\d{2})?)?$|^\d{8}$|^\d{10}$/

const sel = (d?: string, span?: number, from?: string): ScanSel | undefined =>
  d || span || from
    ? { ...(d ? { d } : {}), ...(span ? { span } : {}), ...(from && !span ? { from } : {}) }
    : undefined

export const encodeSel = (v: ScanSel | undefined): string | undefined => {
  if (!v) return undefined
  if (v.invalid) return v.invalid
  const d = encodeScan(v.d) ?? ''
  const before = v.from ? `-${encodeScan(v.from)}` : v.span ? `-${encodeSpan(v.span)}` : ''
  return d + before || undefined
}

/** `?d=`'s slugs, split per the grammar above (step 3–4): `[end, start]`
 * (either may be undefined), or null when the tokens don't form one. */
function selSlugs(head: string): [string | undefined, string | undefined] | null {
  const toks = head.split('-')
  const lead = toks[0] === ''
  const slugs: string[] = []
  for (const [i, t] of toks.entries()) {
    if (i === 0 && lead) continue
    if (SLUG_TOKEN.test(t)) slugs.push(t)
    else if (/^(?:\d{2}|\d{4})$/.test(t) && slugs.length && /^\d{6}$/.test(slugs[slugs.length - 1])) slugs[slugs.length - 1] += `-${t}`
    else return null
  }
  if (lead) return slugs.length === 1 ? [undefined, slugs[0]] : head === '' ? [undefined, undefined] : null
  return slugs.length === 1 ? [slugs[0], undefined] : slugs.length === 2 ? [slugs[0], slugs[1]] : null
}

export const decodeSel = (e: string | undefined, now = new Date()): ScanSel | undefined => {
  if (!e) return undefined
  const invalid: ScanSel = { invalid: e }
  if (/^\d{4}-/.test(e)) { const d = decodeScan(e, now); return d ? { d } : invalid }
  const sm = SPAN_SUFFIX.exec(e)
  const span = sm ? decodeSpan(sm[1]) : undefined
  const head = span ? e.slice(0, sm!.index) : e
  const slugs = selSlugs(head)
  if (!slugs) {
    // a year-less single slug (`10-9`, `8-19-10`), whole
    const d = decodeScan(head, now)
    return d ? sel(d, span) : invalid
  }
  const [end, start] = slugs
  if (start && span) return invalid
  const d = end ? decodeScan(end, now) : undefined, from = start ? decodeScan(start, now) : undefined
  if ((end && !d) || (start && !from)) return invalid
  return sel(d, span, from) ?? invalid
}

/** Fold `?d=` with the legacy keys it replaced — `?date=<id>` (the "after"
 * scan) and `?from=<id>` (a pinned start, /names) — into one selection; `d`'s
 * own parts win. The page then rewrites the URL to `?d=` alone. */
export function mergeSel(vals: { d?: ScanSel; date?: ScanSel; from?: ScanSel }): ScanSel | undefined {
  const { d, date, from } = vals
  // A value that names nothing wins: the page shows the miss rather than a guess.
  const invalid = d?.invalid ?? date?.invalid ?? from?.invalid
  if (invalid) return { invalid }
  return sel(d?.d ?? date?.d, d?.span, d?.from ?? (d?.span ? undefined : from?.from))
}

/** The legacy-key decoders `mergeSel` folds: each reads an ISO or compact id. */
export const legacyDateParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => v ? sel(decodeScan(v, now)) ?? { invalid: v } : undefined,
})
export const legacyFromParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => v ? sel(undefined, undefined, decodeScan(v, now)) ?? { invalid: v } : undefined,
})

/** A selection's "after" scan, resolved: the latest scan matching `d`, or the
 * latest scan when `d` is absent; null when `d` matches nothing, the value is
 * `invalid`, or `scans` is empty — never a fallback to another scan. */
export function resolveAfter(s: ScanSel | undefined, scans: readonly string[], times?: ScanTimes): string | null {
  if (s?.invalid) return null
  if (s?.d) return latestScan(s.d, scans, times)
  return newestScan(scans, times)
}

/** A selection's "before" scan, given its resolved "after": a pinned `from`
 * resolves (latest match) among the scans before `after` — no match is null,
 * a miss, never the nearest; a span (a duration) resolves to the scan nearest
 * `after − span`; neither = undefined (the caller's default, usually the
 * previous scan, or none). */
export function resolveBefore(s: ScanSel | undefined, after: string, scans: readonly string[], times?: ScanTimes): string | null | undefined {
  const earlier = scans.filter(x => scanCmp(x, after, times) < 0)
  if (s?.from) return latestScan(s.from, earlier, times)
  if (s?.span) return nearestScan(earlier, scanInstant(after, times) - s.span, times)
  return undefined
}

/** A URL's scan selection — `?d=` merged with the legacy `?date=` / `?from=`
 * (`mergeSel`). Pure: what `useScanSel` resolves, for code that reads the
 * search string itself (a router's params, an OG view). */
export function selOf(sp: URLSearchParams, now = new Date()): ScanSel | undefined {
  const get = (k: string) => sp.get(k) ?? undefined
  return mergeSel({
    d: decodeSel(get('d'), now),
    date: legacyDateParam(now).decode(get('date')),
    from: legacyFromParam(now).decode(get('from')),
  })
}

/** A URL's scan selection in canonical form (`selOf`, re-encoded compactly),
 * or undefined when it selects nothing (absent); an unparseable value stays
 * verbatim (a miss). Given the scan list (and `times`), each pinned endpoint
 * that names one scan for good is that scan's shortest slug
 * (`canonicalizeSel`): what a page's own URL canonicalizes to. Without one
 * (the OG card's view key, a pure route) the spelling alone is canonicalized. */
export const canonicalSel = (sp: URLSearchParams, now = new Date(), scans?: readonly string[], times?: ScanTimes): string | undefined =>
  encodeSel(scans ? canonicalizeSel(selOf(sp, now), scans, times, now) : selOf(sp, now))

/** The slug a missed selection names, for a "no scan matches" message: the
 * `invalid` value, or the canonical end slug. */
export const selSlug = (s: ScanSel): string => s.invalid ?? encodeScan(s.d) ?? ''
