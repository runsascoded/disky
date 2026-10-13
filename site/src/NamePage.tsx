import { useQuery } from '@tanstack/react-query'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { HotMaps } from './HotMaps'
import { HotSearchForm, HotTotals } from './HotSearch'
import { SiteKbd } from './SiteKbd'
import type { HotRequest } from './hotModel'
import { HexRunsInfo, hexRunsNote } from './FilterNote'
import { loadName, loadNameRegistry, NAME_DIRS_ONLY, namePageParams, nameRequest, nameResultForRegistry, type NameQualification, type NameResult } from './nameModel'
import { useDocTitle } from './title'
import { fmtScan, fromMiss, scanMiss, useScans, useScanSel, type ScanMiss } from './scan'
import { DEFAULT_STORE } from './stores'
import { useFilterCaps } from './filterCaps'
import { hrefWithScan, NoScanMatch } from './NoScanMatch'
import { encodeSel, exactPrefix, isScanId, minPrefix, resolveAfter, resolveBefore, selOf, type ScanSel } from './scanSlug'
import './hot.scss'

export function NamePlanStatus({ result }: { result: NameResult }) {
  const sides = result.before ? [{ view: result.before, execution: result.execution.before! }, { view: result.after, execution: result.execution.after }]
    : [{ view: result.after, execution: result.execution.after }]
  return <div aria-label="Name-summary execution plan">{sides.map(({ view, execution }) => <p className="hot-note" key={view.date}>
    {view.date}: {execution.plan === 'catalog' ? 'Catalog (precomputed)' : 'Bounded name postings (on demand)'}. {execution.source} — {execution.validation.description}
    {execution.dirs_only && <>{' '}<span className="fflag" role="note">{NAME_DIRS_ONLY}</span></>}
  </p>)}</div>
}
/** A registry's domain in words: the threshold, and the short literals registered whatever their frequency. */
export function catalogDomain(registry: NameQualification): string {
  const threshold = registry.threshold_rows !== undefined
    ? `≥${registry.threshold_rows.toLocaleString('en-US')} index rows to answer on demand (${registry.name_rows!.toLocaleString('en-US')} per name)`
    : `≥${registry.threshold_paths!.toLocaleString('en-US')} matching paths`
  return `${threshold}${registry.short_chars ? `, or at most ${registry.short_chars} characters` : ''}`
}
/** The static name index's dispatch in words (`static-name-registry-v1`). */
export function staticDomain({ generation, max_rows }: { generation: string; max_rows: number }): string {
  const v = max_rows.toLocaleString('en-US')
  return `Every scan answers from the static name index (generation ${generation}) with no query server: literals of one or two characters, and literals with more than ${v} index rows, from its precomputed catalog; every other literal from its suffix postings, reading at most ${v} rows.`
}
/** Every date when few; otherwise the count and range (the select lists each one). */
export function scanList(dates: readonly string[]): string {
  return dates.length <= 6 ? dates.join(', ') : `${dates.length} scans, ${dates[0]} to ${dates[dates.length - 1]}`
}
/** The /names selection's misses against the store's scans: the end slug (or
 * an unparseable value), else a pinned baseline naming no earlier scan. */
export function nameMisses(sel: ScanSel | undefined, dates: readonly string[] | undefined): { endMiss: ScanMiss | null; startMiss: ScanMiss | null } {
  if (!dates) return { endMiss: null, startMiss: null }
  const endMiss = scanMiss(sel, dates, true)
  return { endMiss, startMiss: endMiss ? null : fromMiss(sel?.from, resolveAfter(sel, dates), dates) }
}

/** The request params (`name`, ISO `date`/`from`) a /names URL selects: its
 * `?d=` resolved against the STORE's scans (`resolveAfter`/`resolveBefore` —
 * a day is its latest scan, a span the nearest earlier scan), the map's own
 * resolution, so a `d` names the same scan on every page. An unmatched slug
 * passes through as its prefix, so `nameRequest` reports it unavailable. */
export function nameScanParams(url: URLSearchParams, sel: ScanSel | undefined, dates: readonly string[] | undefined): URLSearchParams {
  const out = new URLSearchParams()
  for (const name of url.getAll('name')) out.append('name', name)
  for (const [key, value] of url) if (!['name', 'd', 'date', 'from'].includes(key)) out.append(key, value)
  if (!dates) return out
  const after = resolveAfter(sel, dates) ?? sel?.d
  if (after) out.set('date', after)
  const before = after && isScanId(after) ? resolveBefore(sel, after, dates) : undefined
  const from = before ?? sel?.from
  if (from) out.set('from', from)
  return out
}

/** The URL a /names search writes: the form's ISO `date`/`from` as the
 * canonical `?d=` (a `date` that is the store's latest scan floats, as on the
 * map), then `name`. `dates`: the store's scans. */
/** The resolved scans (`date`, then `from`) the name index doesn't hold yet. */
export function unindexedScans(scanParams: URLSearchParams, indexed: readonly string[] | undefined): string[] {
  if (!indexed) return []
  return [scanParams.get('date'), scanParams.get('from')].filter((d): d is string => !!d && isScanId(d) && !indexed.includes(d))
}

export function nameUrlParams(form: URLSearchParams, dates: readonly string[] | undefined, now = new Date()): URLSearchParams {
  const date = form.get('date') ?? undefined, from = form.get('from') || undefined
  const latest = dates && resolveAfter(undefined, dates)
  // each a picked scan id, written as its canonical prefix among the store's scans (`minPrefix`; no starts
  // here, so a date-only scan sharing its day is its midnight), or its minute before they answer
  const pin = (id: string) => (dates ? minPrefix(id, dates, {}, now) : exactPrefix(id))
  const d = encodeSel({ ...(date && date !== latest ? { d: pin(date) } : {}), ...(from ? { from: pin(from) } : {}) })
  return new URLSearchParams({ ...(d ? { d } : {}), name: form.get('name') ?? '' })
}

export function NamePage() {
  useDocTitle('Name summaries preview')
  const { rootLabel } = useFilterCaps()
  const [rawParams, setParams] = useSearchParams()
  const location = useLocation()
  // The scan selection is the map's `?d=` (one key and codec on every page):
  // `useScanSel` rewrites a legacy `?date=`/`?from=` (or ISO) link to it on
  // mount; the page reads the router's params through the same merge.
  useScanSel()
  const sel = selOf(rawParams)
  const registry = useQuery({ queryKey: ['name-summary-registry'], queryFn: ({ signal }) => loadNameRegistry(signal), staleTime: Infinity, retry: false })
  const dates = registry.data?.dates.map(row => row.date)
  // `?d=` resolves against the store's scans, as on the map; the name index may lag them.
  const scansQ = useScans(DEFAULT_STORE)
  const storeScans = scansQ.data
  const scanParams = nameScanParams(rawParams, sel, storeScans)
  const params = namePageParams(scanParams)
  let request: HotRequest | undefined, issue: string | undefined
  // A slug naming no store scan is a miss: say so and offer the closest
  // scans; never answer for another scan.
  const { endMiss, startMiss } = nameMisses(sel, storeScans)
  const miss = endMiss ?? startMiss
  // A store scan the name index doesn't hold yet: say so; never answer for the index's nearest scan.
  const unindexed = miss ? [] : unindexedScans(scanParams, dates)
  const latestIndexed = dates && resolveAfter(undefined, dates)
  if (dates && storeScans && !registry.error && !miss && !unindexed.length) try { request = nameRequest(scanParams, dates) } catch (error) { issue = (error as Error).message }
  const query = useQuery({ queryKey: ['name-summary', request?.date, request?.name, request?.from], queryFn: async ({ signal }) => {
    return nameResultForRegistry(await loadName(request!, signal, dates), registry.data!)
  }, enabled: !!request, staleTime: Infinity, retry: false })
  let result: NameResult | undefined
  if (request && query.data) try { result = nameResultForRegistry(query.data, registry.data!) } catch (error) { issue = (error as Error).message }
  const consolidated = registry.data?.dates.filter(row => [request?.date, request?.from].includes(row.date) && row.kind === 'consolidated-store-v1') ?? []
  const uncataloged = consolidated.filter(row => !row.plans.includes('catalog')), cataloged = consolidated.filter(row => row.plans.includes('catalog'))
  const catalogOnly = registry.data?.dates.filter(row => [request?.date, request?.from].includes(row.date) && row.kind === 'daily-scalar-source-v1' && !row.plans.includes('bounded-name-postings')) ?? []
  return <main className="hot-page">
    <header><Link to="/">{rootLabel ?? 'Map'}</Link><h1>Name search — exact root summaries</h1>{dates && !registry.error && <p>Available scans: {scanList(dates)}.</p>}</header>
    <p className="hot-scope">Case-insensitive literal substring within any path component name; no slash-crossing. Matching directories cover their descendants, counted once. Exact bytes and object counts, including zero-byte objects. A leading ^ or trailing $ is an anchor, which this page doesn’t search; write \^ or \$ (or quote it) to search them literally.</p>
    {dates && !registry.error && <HotSearchForm key={rawParams.toString()} params={params} dates={registry.data?.dated ? dates : undefined} onSearch={next => setParams(nameUrlParams(next, storeScans))} />}
    {registry.data && !registry.error && <p id="hot-availability" className="hot-note">{registry.data.static ? staticDomain(registry.data.static)
      : catalogOnly.length
      ? `${catalogOnly.map(row => `${row.date}: catalog literals only, using membership qualified on ${row.qualification_dates!.join(', ')}`).join('. ')}—not a current-scan frequency claim. Other literals are unavailable for those scans, not zero matches; no on-demand fallback.`
      : uncataloged.length ? `${uncataloged.map(row => row.date).join(' and ')}: no prepared catalog; every literal is answered on demand from the consolidated name index, and requests exceeding the work budget fail explicitly, not as zero matches.`
      : cataloged.length ? `${cataloged.map(row => row.date).join(' and ')}: literals registered on the scan itself (${catalogDomain(cataloged[0].registry!)}) use the consolidated catalog; others are below that threshold and answer on demand from the consolidated name index, and requests exceeding the work budget fail explicitly, not as zero matches.`
      : 'Catalog literals use prepared summaries. Other literals use bounded name postings on demand; requests exceeding the work budget fail explicitly, not as zero matches. No unbounded fleet scan.'}</p>}
    <p className="hot-note">With a baseline, coverage is computed for each snapshot; change is the selected scan minus the baseline, not only paths that changed.</p>
    {!registry.error && unindexed.length > 0 && <p className="no-scan loading" role="alert" aria-label="Scan not indexed">
      Scan {unindexed.map(d => fmtScan(d)).join(' and ')} is not in the name index yet (it is indexed after each scan lands). This is not a zero-match result.
      {latestIndexed && <>{' '}Latest indexed: <Link to={hrefWithScan(location.pathname, location.search, sel, latestIndexed)}>{fmtScan(latestIndexed)}</Link></>}
    </p>}
    {miss && <NoScanMatch what={endMiss ? 'scan' : 'baseline scan'} miss={miss} hrefFor={scan => hrefWithScan(location.pathname, location.search, sel, scan, !!endMiss)} />}
    {issue && <p role="alert">{issue}</p>}
    {(registry.isPending || scansQ.isPending) && <p role="status">Loading available name-summary scans…</p>}
    {registry.error && <p role="alert">{registry.error.message}</p>}
    {scansQ.error && <p role="alert">Couldn’t load the scan list ({scansQ.error.message}), so this page can’t tell which scan the link names.</p>}
    {request && !issue && query.isPending && <p role="status">Loading exact name summary…</p>}
    {request && !issue && query.error && <p role="alert">{query.error.message}</p>}
    {request && !issue && result?.hexRuns && <p className="hot-note hex-runs" aria-label="Hex-run note"><HexRunsInfo min={result.hexRuns.min} /> {hexRunsNote(result.hexRuns.min)}</p>}
    {request && !issue && result && <><NamePlanStatus result={result} /><HotMaps result={result} /><HotTotals result={result} />
      <p className="hot-note">{result.capabilities ? 'These dated root summaries have exact bucket totals only; no prepared bucket detail or deeper drill-down.' : 'This on-demand result has root and bucket totals only; no prepared bucket detail or deeper drill-down.'}</p></>}
    <footer>No owner or Boolean filters in this preview. The main <Link to="/">storage map</Link> is unchanged.</footer>
    <SiteKbd />
  </main>
}
