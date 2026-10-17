import { Explain } from './Help'
import { keepPreviousData, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useNavigationType } from 'react-router-dom'
import { MdLayers } from 'react-icons/md'
import { useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { bareEmpty, legacyOwner, ownerParam } from './ownerParam'
import { AGE_MODES, AgeChart } from './AgeChart'
import { canonId, shortName, shortUserKey } from './UserChip'
import { signInUrl, useCanAssign, useCanStage, useIdent as useIdentity } from './auth'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap, DiffHeader, useDiffModel } from './DiffTreemap'
import { DiffTable } from './DiffTable'
import { Allowances } from './Allowances'
import { DiskSpace } from './DiskSpace'
import { ExtLink } from './ExtLink'
import { DiskSpaceDiff, diskDiffScan } from './DiskSpaceDiff'
import type { DiffData } from './diffModel'
import { ScanCombobox } from './ScanCombobox'
import { ago, buildUserIndex, epochDaysToDate } from './colors'
import { ChildrenTable } from './ChildrenTable'
import { FitSelect } from './FitSelect'
import { PathPopover } from './PathPopover'
import { fromUrlPath, pathCopy, pathDisplay, pathText, toUrlPath, toUrlSegs } from './pathCrumbs'
import { listsObjects, openHref } from './objects'
import { Busy, Skeleton } from './Busy'
import { useRules } from './rules'
import { useHashSpy } from './hashSpy'
import { useScrollSpyPref } from './prefs'
import { SectionAnchor } from './SectionAnchor'
import { barControls } from './pageBar'
import { LifecycleFold } from './LifecycleFold'
import { ClassMixTip, Tooltip } from './Tooltip'
import { LaptopIndicator } from './laptop'
import { Treemap } from './Treemap'
import type { DateRange, Highlight, ShadeMode } from './Treemap'
import { dateAxis, useDateScale } from './dateScale'
import type { DayBand } from './dayBand'
import { DEFAULT_SYNTAX, SYNTAXES, syntaxById } from './filterTree'
import { fetchCover, matchedListOf, prefetchCover, rowCover, rowMatchesOf, useFilterCover } from './filterCover'
import { HttpError } from './batches'
import { budgetCutRefetch } from './budgetCut'
import type { RowSource } from './MatchActions'
import { type MatchFields, seriesMatches } from './filterMatches'
import { QueryHelpTip } from './QueryHelp'
import { apiError, indexedSyntax, refusalOf, useFilterCaps, useIndexedScans } from './filterCaps'
import { RootCrumb } from './RootCrumb'
import { actBlock, noMatches } from './matchAct'
import { type BlindPanel, blindNote, blindShown } from './filterBlind'
import { LoadFailure, mapSlot } from './LoadFailure'
import { REJECT_MESSAGES, rejectQuery } from '../functions/_lib/indexedOnly'
import { FilterFlags, FilterNote, matchedNote } from './FilterNote'
import { UnionScanPicker } from './UnionScanPicker'
import { UnionMissingNote, UnionRefusals } from './UnionNotes'
import { cutoffOfScan, marksOf, scopedPath, UnionMarks, useUnionSources, type UnionVector } from './unionSources'
import { encodeCutoff } from './federatedScans'
import { BulkBar } from './BulkBar'
import { setCurrentScan, useMyUserState, useOwnerIndex, useOwners } from './owners'
import { ledgerRevOf, ownerScope, ownerScopeReady, pageLedgerKey, scopeKeyOf } from './viewScope'
import { useRegistryLoaded } from './identities'
import { applyLedger } from './ledgerOverlay'
import { MultiSelect } from './MultiSelect'
import { SiteNav, topbarH } from './SiteNav'
import { canvasWidth } from './canvas'
import type { MenuEntry } from './SiteNav'
import { DAY, encodeScan, fmtTimestamp, fromMiss, pendingNote, latestScan, nearestScan, noScansYet, scanAtInstant, scanInstant, useScan } from './scan'
import { scanCmp } from './scanSlug'
import { hrefWithScan, NoScanMatch } from './NoScanMatch'
import { selOf } from './scanSlug'
import { SizeOverTime } from './SizeOverTime'
import { useStore, useStoreFetch } from './store'
import { listingTime } from './listingTime'
import { knownBucket, storeDefaultColor } from './stores'
import { colorModeCodec as modeCodec, mapColorMode } from './colorMode'
import { perf } from './perf'
import { useDocTitle } from './title'
import { TypedPrefixModal } from './TypedPrefix'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_COLORS, CLASS_NAMES, CLASS_PRICE_US, MODE_KEYWORDS, MODE_LABELS, classMix, fmtN, fmtUsd, ratePerByte } from './types'
import { SiteKbd } from './SiteKbd'
import { useUnits } from './units'
// The leaf viewer (file-tree's renderers, hyparquet…): its own chunk, fetched
// the first time an object opens.
const ObjectPanel = lazy(() => import('./ObjectPanel'))
// The color axes on offer.
const MODES: ColorMode[] = ['read', 'user', 'date', 'tree']

/**
 * URL value codecs. Values are ONE letter on the wire (`?c=t`); every older
 * spelling still decodes (`tree`, `written`/`age`, `class`…) so old links
 * keep working, and `useCanonicalParams` rewrites them to the short form on
 * load. `use-prms` has no alias support of its own — a codec's `decode`
 * accepts the legacy forms and the rewrite is ours. (The retired mark
 * coloring — `m`/`marks`/`mark`/`fate` — decodes to nothing: the default.)
 */
const shadeCodec = {
  encode: (v: string | undefined) => (v === 'class' ? 'c' : undefined),
  decode: (e: string | undefined): string | undefined => (e === 'c' || e === 'class' ? 'class' : undefined),
}
/** Rewrite legacy param spellings to their canonical encoding, once, on load. */
function useCanonicalParams(codecs: [string, { encode: (v: string | undefined) => string | undefined; decode: (e: string | undefined) => string | undefined }][]) {
  useEffect(() => {
    const q = new URLSearchParams(window.location.search)
    let changed = false
    for (const [k, codec] of codecs) {
      const raw = q.get(k)
      if (raw === null) continue
      const canon = codec.encode(codec.decode(raw))
      if (canon === raw) continue
      changed = true
      if (canon === undefined) q.delete(k)
      else q.set(k, canon)
    }
    if (changed) window.history.replaceState(window.history.state, '', `${window.location.pathname}${q.size ? `?${q}` : ''}${window.location.hash}`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}

// The storage-class axis (`?cl=`): one letter per class, in class order.
type ClassAxis = 's' | 'n' | 'c' | 'a'
const CLASS_AXES: ClassAxis[] = ['s', 'n', 'c', 'a']
const CLASS_OF: Record<ClassAxis, string> = { s: '1', n: '2', c: '3', a: '4' }

// Diff-section span presets (days back from the "after" scan).
const SPANS: [string, number][] = [['1d', 1], ['3d', 3], ['7d', 7], ['14d', 14], ['30d', 30]]

// The owner axis: `?o` is the unowned pool, `?o=*` the owned one, `?o=me`
// or a user key (`?o=rw`) a person (`ownerParam`); absent = everything. Owned = a person owns it (inferred from
// paths/runs, or assigned); unowned
// = the nobody-owns-it pool. A user narrows "owned" to that person (`OwnerMode`, `viewScope.ts`).

// Home-page section anchors, top to bottom — the scroll-spy keeps `#hash`
// tracking the one in view, and deep links scroll to it. Old ids keep working.
// Client cache version: appended to the read endpoints' URLs so a browser's
// HTTP copy of a pre-deploy answer (they were kept a day until 2026-09-17,
// five minutes since) is never replayed after a reader rule changes. Bump
// with `CACHE_V` in functions/_lib/edgeCache.ts.
const API_CV = '2'
const SECTION_IDS = ['tree-map', 'tbl', 'over-time', 'diff', 'mtime']
const LEGACY_ANCHORS: Record<string, string> = {
  'size-over-time': 'over-time', 'created-date': 'mtime', changes: 'diff',
}

function AppContent() {
  // Which object store to render: the subtree's <StoreProvider> (Root mounts
  // one per configured store under its path; the primary is the default).
  // Every data request goes through `sfetch`, which carries `store=<key>` for
  // a secondary store and is the global fetch for the primary.
  const location = useLocation()
  const { pathname, search } = location
  const navType = useNavigationType()
  const navigate = useNavigate()
  const store = useStore()
  const time = listingTime(store.scheme)
  const sfetch = useStoreFetch()
  // A union view (`STORES_JSON` `union`): its members' scans drive the picker, their marks the map's provenance.
  const union = useUnionSources(store)
  const unionMarks = useMemo(() => marksOf(union), [union])
  // Legacy `?path=<prefix>` links (cw-s3 drilled by query param until the
  // union): forward to the URL-path form, keeping the other params.
  useEffect(() => {
    const q = new URLSearchParams(search)
    const legacy = q.get('path')
    if (legacy == null) return
    q.delete('path')
    const base = store.path === '/' ? '' : store.path
    const rest = q.toString()
    navigate({ pathname: legacy ? `${base}/${legacy.replace(/^\/+|\/+$/g, '')}` : store.path, search: rest ? `?${rest}` : '', hash: window.location.hash }, { replace: true })
  }, [search, navigate, store])
  const canAssign = useCanAssign()
  const canStage = useCanStage()
  const ident = useIdentity()
  // The owner axis (`Store.owners`): the ownership ledger overlays the map
  // and the table for any signed-in viewer; admins assign from it.
  const ownersMode = store.owners && !!ident
  const ownersQ = useOwners(ownersMode)
  const ownerIdx = useOwnerIndex(ownersQ.data)
  const [typedOpen, setTypedOpen] = useState(false)
  // Keep the tab title in sync with the store on client-side navigation.
  useDocTitle() // the bare site name (= the store's title) is the home page
  // URL token matches the visible label ("written"), not the internal key
  // ("date"); old ?c=age links still decode (the retired axes decode to the
  // default).
  // ABSENT is meaningful: it means "the lens-appropriate default" (see `mode`
  // below), so switching lenses re-defaults the coloring — but an explicit
  // pick (any `?c=`) survives every lens change.
  const [modeP, setModeP] = useUrlState('c', modeCodec)
  // The age chart's own color axis (`?ac=`, same tokens); absent = follow the map.
  const [ageModeP, setAgeModeP] = useUrlState('ac', modeCodec)
  useCanonicalParams([['c', modeCodec], ['ac', modeCodec], ['s', shadeCodec]])
  // Scan selection (`?d=YYMMDD`) + the polling scan list, shared with /users
  // and /user/:id via useScan (specs/done/scan-param-all-pages.md). Absent `?d` is
  // a first-class "latest", so a parked tab follows new scans.
  // A filtered view with no `?d=` on an indexed-only deployment opens on the newest scan the static
  // index covers (a just-published scan isn't searchable until the index appends it), and says so.
  const { indexedOnly: indexedOnly0 } = useFilterCaps()
  const [fFloat] = useUrlState('f', stringParam())
  const indexedScans = useIndexedScans(indexedOnly0 && !!fFloat)
  const { asof, miss, pending, scans, times, label: fmtS, dMatches, dP, setDP, span, setSpan, from, setFrom, setEndPin, setRange, at, setAt, atFrom, setFromAt, scansQ } = useScan(store, indexedOnly0 && fFloat ? indexedScans : undefined)
  const rulesQ = useRules()
  const rules: Rules | null = rulesQ.data ?? null
  // Ledger actions record which scan the actor was viewing.
  useEffect(() => setCurrentScan(asof ?? undefined), [asof])
  const scanQuery = <T,>(name: string, scan = asof) => ({
    queryKey: [name, store.key, scan],
    queryFn: () => sfetch(`${store.base}/${scan}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!scan,
    staleTime: Infinity,
  })
  // `?f=` (name filter) and `?o=` (owner axis): the page's scope, sent to the
  // server with every view (see `scopeQs` below). The owner axis replaces the
  // old review *lenses* (`?l=user|unclaimed`, `?lu=`, and the `?u=` legend
  // pin). Old links normalize below.
  const [fqRaw, setFq] = useUrlState('f', stringParam())
  // `?qs=`: the filter's syntax (`functions/_lib/querySyntax.ts`), over the
  // store's default. The box parses with it as it goes: a query that doesn't
  // parse (or an unknown `?qs=`) is shown under the box and not applied, so
  // `fq` below is the applied filter (undefined: none).
  const [qsP, setQsP] = useUrlState('qs', stringParam(), true)
  const storeSyntax = syntaxById(store.querySyntax ?? '') ?? DEFAULT_SYNTAX
  // An indexed-only deployment (`FILTER_INDEXED_ONLY`): its one syntax, `plain` (every character literal; `?qs=`
  // doesn't apply); what the static index can't answer yet is refused inline, never sent.
  const { indexedOnly, pathTerms = false, rootLabel, rootTitle } = useFilterCaps()
  const syntax = indexedOnly ? indexedSyntax(pathTerms) : (qsP && syntaxById(qsP)) || storeSyntax
  const boxSyntax = syntax
  const fParse = useMemo((): { ok: boolean; error?: string } => {
    if (!indexedOnly && qsP && !syntaxById(qsP)) return { ok: false, error: `unknown query syntax '${qsP}' (want ${SYNTAXES.map(x => x.id).join('|')})` }
    if (!fqRaw) return { ok: false }
    const refused = indexedOnly ? rejectQuery(fqRaw, pathTerms) : null
    if (refused) return { ok: false, error: refused.message }
    const r = syntax.parse(fqRaw)
    return r.error !== undefined ? { ok: false, error: r.error } : { ok: !!r.ast }
  }, [fqRaw, qsP, syntax, indexedOnly, pathTerms])
  // The box edits a local draft; the URL (and every query keyed on it) follows
  // after a 250 ms pause — one request pair per phrase, not per keystroke.
  const [fqDraft, setFqDraft] = useState<string | null>(null)
  // Brushing: the view's child (by name) hovered in either the treemap or its
  // children table, lit in both (a ring on the cell, the row's hover tint).
  const [brush, setBrush] = useState<string | null>(null)
  // The date brush shared by the map's gradient key and the age chart's bars.
  const [band, setBand] = useState<DayBand | null>(null)
  useEffect(() => {
    if (fqDraft == null) return
    const t = setTimeout(() => { setFq(fqDraft || undefined); setFqDraft(null) }, 250)
    return () => clearTimeout(t)
  }, [fqDraft, setFq])
  // Lens changes push history (they change WHAT you're looking at, like a
  // drill); cosmetics (`?c=`, `?s=`, `?n=`) replace.
  const [oP, setOP] = useUrlState('o', ownerParam, true)
  // `?by=<assigner>` — the /assignments heatmap cell lens: with a user owner
  // lens, fold only the assignments that assigner made. Only meaningful alongside a
  // person in `?o=`.
  const [byP] = useUrlState('by', stringParam())
  // `?s=` — the secondary "shade by" axis, a perturbation within each cell's
  // primary color (`ShadeMode`). Absent = none: the primary axis unchanged.
  const [sP, setSP] = useUrlState('s', shadeCodec)
  const shade: ShadeMode = sP === 'class' ? 'class' : 'none'
  // `?cl=` ⊆ snca — the storage-class axis (server-side): the view's bytes
  // are cut to the allowed classes. All four = no scope.
  const [clP, setClP] = useUrlState('cl', stringParam(), true)
  const classSet = useMemo((): ReadonlySet<ClassAxis> | null => {
    const on = new Set(CLASS_AXES.filter(c => (clP ?? '').includes(c)))
    return on.size > 0 && on.size < CLASS_AXES.length ? on : null
  }, [clP])
  const setClasses = (ks: ClassAxis[]) => setClP(ks.length === 0 || ks.length === CLASS_AXES.length ? undefined : CLASS_AXES.filter(c => ks.includes(c)).join(''))
  const { user: myUser, ready: myUserReady } = useMyUserState(ident?.email, ownersMode)
  // Owner axis (`viewScope.ts`). `me` resolves to the signed-in user's attribution id (a
  // shared `?o=me` link shows each reader their own files); an unmapped
  // email resolves to nothing, and the axis falls back to "all" with a note.
  // `?o=!<key>,<key>` — the "owned by someone OTHER than these people" pool
  // (the sweep console's "show me the conflicts under this band" link). The
  // excluded users resolve to canonical ids for the server's row filter.
  const owner = ownerScope({ ownersMode, oP, byP, myUser, canon: canonId })
  const { notUsers, ownerUser, ownerMode, meUnmapped, activeLens, assigner } = owner
  // Its params wait on the registry (a golfed key) and the viewer's id (`me`): the view's queries hold
  // until they're final, so a cold `?o=…` load sends each request once.
  const registryLoaded = useRegistryLoaded()
  const scopeReady = ownerScopeReady({ ownersMode, oP, byP, registryReady: registryLoaded || rulesQ.isError, myUserReady })
  const setOwnerUser = (u: string | undefined) => setOP(u === undefined ? undefined : u === 'me' ? 'me' : shortUserKey(canonId(u)))
  // Flip a picked person between their own bytes (`?o=<key>`) and everyone
  // else's under the current view (`?o=!<key>`) — the ≠ toggle beside the picker.
  const negateOwner = (on: boolean) => {
    const key = ownerUser ? shortUserKey(ownerUser) : notUsers.length ? shortUserKey(notUsers[0]) : null
    if (key) setOP(on ? `!${key}` : key)
  }
  // Every scope axis is applied server-side by /api/subtree (specs/
  // view-serving.md §2): a user (`lens=user:`, the live assignments folded in), a
  // pool (`o=`), the classes (`cl=`), the name filter (`q=`). The client
  // receives exactly the current view and only draws it.
  // …and under an owner pool (the static index's hits are owner slices), not a person or a class scope (refused there too).
  const fScopeRefused = indexedOnly && !!fqRaw && fParse.ok && (!!activeLens || !!classSet) ? REJECT_MESSAGES['unsupported-scope'] : undefined
  const fq = fParse.ok && !fScopeRefused ? fqRaw : undefined
  const scopeQs =
    owner.qs +
    (classSet ? `&cl=${CLASS_AXES.filter(c => classSet.has(c)).join('')}` : '') +
    (fq ? `&q=${encodeURIComponent(fq)}&qs=${syntax.id}` : '')
  // A user lens or an owner pool folds the live ledger server-side, so those
  // views' queries carry its revision: an assignment (or its undo) refetches
  // them; everything else ignores the ledger. The ledger's first load is not a
  // change (`pageLedgerKey`): a view fired before `/api/actions` lands keeps its key.
  const ledgerRev = useMemo(() => {
    let max = 0
    for (const r of ownerIdx.owners.values()) max = Math.max(max, r.action_id)
    return ledgerRevOf(ownerIdx.count, max)
  }, [ownerIdx])
  const ledger = pageLedgerKey(ledgerRev, ownersQ.data !== undefined)
  const scopeKey = scopeKeyOf(scopeQs, owner.ledgered, ledger)
  // One-time legacy-param rewrite onto the two axes, so old links (Slack
  // digests, /user pages) work and re-share in the current form:
  //   ?l=todo → ?k=u · ?l=unclaimed|communal, ?t=unattributed|communal → bare ?o
  //   ?o=unowned|unclaimed → bare ?o · ?o=owned|claimed → ?o=*
  //   ?l=user[&lu=x] (and older ?mt=mine[&mu=x]) → ?o=x|me · ?u=x (legend pin) → ?o=x
  //   any other ?t= (the retired group pin) → dropped
  useEffect(() => {
    const sp = new URLSearchParams(search)
    const legacy = ['l', 'lu', 'u', 'mt', 'mu', 't']
    const t = sp.get('t')
    // A pool spelled long (`unowned` / `owned`) or retired (`unclaimed` /
    // `claimed`, until 2026-09-07) → its short form (bare `o` / `o=*`).
    const oldPool = legacyOwner(sp.get('o'))
    if (!legacy.some(k => sp.has(k)) && oldPool === null) return
    if (oldPool !== null) sp.set('o', oldPool)
    const l = sp.get('l') ?? sp.get('mt')
    const lu = sp.get('lu') ?? sp.get('mu')
    const u = sp.get('u')
    for (const k of legacy) sp.delete(k)
    if (t === 'unattributed' || t === 'communal') sp.set('o', '')
    if (l === 'todo') sp.set('k', 'u')
    else if (l === 'unclaimed' || l === 'communal') sp.set('o', '')
    else if (l === 'user' || l === 'mine') sp.set('o', lu ? shortUserKey(canonId(lu)) : 'me')
    if (u && !sp.has('o')) sp.set('o', shortUserKey(canonId(u)))
    navigate({ pathname, search: `?${bareEmpty(sp.toString(), 'o')}`, hash: window.location.hash }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])
  const metaQ = useQuery(scanQuery<Meta>('meta'))
  // The Diff section's "before" endpoint comes from the `?d=` span (see
  // scan.ts): absent = the previous scan; a span resolves to the scan
  // *nearest* that far before "after" — scan times drift minutes past exact
  // multiples, so "at least N days back" would skip half a cadence. The
  // "after" endpoint IS the page's scan (`?d=`). Both sides align client-side
  // from `/api/subtree` at the drilled path and take the page scope (lens,
  // pinned row, `?f=`) exactly like the map — one code path for every scope;
  // the batch job's root-only `diff.json` is no longer read here.
  const prevScan = asof ? scans[scans.indexOf(asof) + 1] ?? null : null
  const earlier = useMemo(() => (asof ? scans.filter(s => scanCmp(s, asof, times) < 0) : []), [asof, scans, times])
  const spanScan = span && asof ? nearestScan(earlier, scanInstant(asof, times) - span, times) : null
  // A pinned start (`from`) wins over a look-back span; both fall back to the
  // immediately-previous scan. `from` is a slug: the latest earlier scan it
  // matches (the resolver). One matching none is a miss — the diff says so
  // and draws nothing, never the nearest scan instead.
  const fromScan = from && asof ? latestScan(from, earlier, times) : null
  const startMiss = useMemo(() => fromMiss(from, asof, scans, times), [from, asof, scans, times])
  // `?atf=`: the start as a cutoff instant; the page's scan for it is the newest earlier one at or before it.
  const diffPrev = startMiss ? null : atFrom ? scanAtInstant(earlier, atFrom, times) : fromScan ?? spanScan ?? prevScan
  // A union view's cutoffs as instants (`?at=`/`?atf=`, `pickCutoff`): its endpoints read them exactly (each member
  // at its newest scan by then). A single store reads the page's scans for them (`scanAtInstant`), as ever.
  const endAt = union && at ? encodeCutoff(at) : null
  const startAt = union && atFrom ? encodeCutoff(atFrom) : null
  const cutQs = endAt ? `at=${endAt}` : `date=${asof}`
  const cutKey = endAt ?? asof
  const diffQs = `${startAt ? `from_at=${startAt}` : `from=${diffPrev}`}&${endAt ? `to_at=${endAt}` : `to=${asof}`}`
  const diffKey = `${startAt ?? diffPrev}→${endAt ?? asof}`
  // A store whose scans carry physical disk readings (`meta.disk_space`) compares them across the diff;
  // any other store never fetches the earlier scan's meta (`diskDiffScan`).
  const beforeMetaQ = useQuery({
    ...scanQuery<Meta>('meta', diffPrev),
    enabled: diskDiffScan(metaQ.data?.disk_space, diffPrev) != null,
  })
  // Hour-rounded span back from `to` — the previous scan clears it, anything
  // else round-trips as its own span (nearest-scan resolution recovers it,
  // and the link keeps following `latest`).
  const spanTo = (to: string, from: string): number | undefined =>
    scans[scans.indexOf(to) + 1] === from
      ? undefined
      : Math.max(3600_000, Math.round((scanInstant(to, times) - scanInstant(from, times)) / 3600_000) * 3600_000)
  // A brush on the size chart hands back the scan ids its ends sit on (else a
  // prefix, which resolves to the latest scan matching it — never a date-only
  // id read as its whole day), and the pair becomes the page's `?d=` (after + span).
  const brushScan = (v: string) => (scans.includes(v) ? v : latestScan(v, scans, times))
  const brushRange = (from: string, to: string) => {
    const toScan = brushScan(to)
    const fromScan = brushScan(from)
    if (!toScan || !fromScan || scanCmp(toScan, fromScan, times) <= 0) return
    setRange(toScan, spanTo(toScan, fromScan), fromScan)
  }
  const diffWindow: [string, string] | undefined = diffPrev && asof ? [diffPrev, asof] : undefined
  // Two orthogonal, low-key toggles for the diff window (see scan.ts grammar):
  // the start is either a pinned scan (`from`) or a look-back span; the end
  // either follows the latest scan (floating) or is pinned. The end toggle only
  // means anything while the page IS on the latest scan (an older `asof` is
  // already pinned), so it hides otherwise.
  const startPinned = from !== undefined
  const endIsLatest = !!asof && asof === scans[0]
  const endPinned = dP !== undefined
  // Presets past the history's reach — nearest scan more than a quarter of
  // the span off, or already assigned by a shorter preset — are dropped
  // rather than mislabeled.
  const spanPicks = useMemo(() => {
    if (!asof) return []
    const t0 = scanInstant(asof, times)
    const picks: { label: string; ms: number; scan: string }[] = []
    for (const [label, days] of SPANS) {
      const ms = days * DAY
      const best = nearestScan(earlier, t0 - ms, times)
      if (!best || Math.abs(scanInstant(best, times) - (t0 - ms)) > ms / 4) continue
      if (!picks.some(p => p.scan === best)) picks.push({ label, ms, scan: best })
    }
    return picks
  }, [asof, earlier, times])
  // Lazy drill (specs/done/path-index-lazy-drill.md step 3, now the primary
  // source): the map's base is the pixel-budget subtree at the store root,
  // and every level of the drilled path gets its own subtree query, grafted
  // in depth order — interactive drills hit each level's cache as they go,
  // and a cold deep link fans the whole chain out in parallel.
  // The URL's `~` (a store home) expanded: `/~/c` → `Users/ryan/c`; each segment decoded once.
  const graftPath = fromUrlPath(pathname.slice((store.path === '/' ? '' : store.path).length), store.home).join('/')
  const canW = canvasWidth(typeof window === 'undefined' ? 1280 : window.innerWidth)
  // Perf-mark keys (`perf.ts`): what tells one load of a widget from another
  // on this page — path, scan(s), canvas width, scope.
  const viewKey = (p: string, d: string | null | undefined, extra = '') => `${p || '/'}@${d}|w${canW}${scopeQs}${extra}`
  const subtreePaths = useMemo(() => {
    const segs = graftPath.split('/').filter(Boolean)
    return ['', ...segs.map((_, i) => segs.slice(0, i + 1).join('/'))]
  }, [graftPath])
  const subtreeQs = useQueries({
    queries: subtreePaths.map(p => ({
      queryKey: ['subtree', store.key, cutKey, p, canW, scopeKey],
      enabled: !!asof && scopeReady,
      staleTime: Infinity,
      // A budget-cut answer (`budgetCut`) is upgraded at the edge in the background: ask again until whole.
      refetchInterval: (q: { state: { data?: { budgetCut?: boolean }; dataUpdateCount: number } }) => budgetCutRefetch(q.state.data, q.state.dataUpdateCount),
      // Retry transient failures, but not the deterministic ones (409: no
      // user index for this scan; 413: view too wide) — those surface as-is.
      retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
      retryDelay: (n: number) => 400 * 2 ** n,
      // With a filter the full read plans each match root's tier (`full=1`);
      // the companion below paints the coarsest-tier forest first.
      queryFn: async ({ signal }: { signal?: AbortSignal }) => {
        const pf = perf.start('treemap', viewKey(p, asof, fq ? '|full' : ''), ['table'])
        const r = await pf.track(sfetch(
          `/api/subtree?cv=${API_CV}&${cutQs}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}${fq ? '&full=1' : ''}`,
          { credentials: 'include', signal },
        ))
        if (!r.ok) { pf.fail(); throw apiError(r.status, await r.text()) }
        const j = await r.json() as MatchFields & { tree: TreeNode; tier?: string; matches?: string[]; threshold?: number; partialReason?: string; approximateReason?: string; hexRuns?: { min: number; tail: number }; budgetCut?: true; union?: UnionVector }
        pf.decoded()
        return j
      },
    })),
  })
  // Progressive fill: a companion `depth=1` fetch per path — the same pixel
  // budget, capped one level below the root, so it returns the *identical*
  // top-level children (branches arrive without `c`, already drillable) from
  // a single depth-band read. It stands in until the full tree lands; because
  // the top level matches, the fill-in adds children under tiles that don't
  // move. The whole held tree (`mapTree`, below) outranks it on later loads,
  // so a scope change never downgrades a held full tree to a coarse one.
  const coarseQs = useQueries({
    queries: subtreePaths.map((p, i) => ({
      queryKey: ['subtree', store.key, cutKey, p, canW, scopeKey, 'depth1'],
      // Deepest path only — see `dataFor`; ancestors use it only under a filter, where it is the
      // exact forest of match roots (cheap from the static name index).
      enabled: !!asof && scopeReady && (i === subtreePaths.length - 1 || !!fq),
      staleTime: Infinity,
      retry: false,
      // Plain view: one depth band. Filtered view: the whole forest from the
      // coarsest tier (`partial`, milliseconds) — the fast first paint of
      // specs/filter-views.md, replaced by the planned-tier read above.
      queryFn: async ({ signal }: { signal?: AbortSignal }) => {
        const pf = perf.start('treemap', viewKey(p, asof, fq ? '|coarse' : '|d1'), ['table'])
        const r = await pf.track(sfetch(
          `/api/subtree?cv=${API_CV}&${cutQs}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}${fq ? '' : '&depth=1'}${scopeQs}`,
          { credentials: 'include', signal },
        ))
        if (!r.ok) { pf.fail(); throw new Error(`${r.status}`) }
        const j = await r.json() as MatchFields & { tree: TreeNode; tier?: string; partialReason?: string; approximateReason?: string; hexRuns?: { min: number; tail: number }; union?: UnionVector }
        pf.decoded()
        return j
      },
    })),
  })
  /** The full tree for path i once it's here; for the DEEPEST path only, the
   * depth-1 one while it isn't. An ancestor must be full or absent: `mapPath`
   * walks the spine by segment and truncates at the first node without
   * children, so a depth-1 ancestor would collapse the drill to itself and
   * the controlled path would thrash until the full spine landed. The
   * deepest node's children are the visible map — the walk never descends
   * through them, and that's the level whose speed matters. */
  // Full > depth-1 (deepest only). Never a *held* previous tree here: a graft
  // must receive a node's own subtree. Holding, say, the previous sibling's
  // tree under a new name puts this node's totals over that node's children,
  // and children that don't sum to their parent send the core's fold
  // arithmetic (`(other)` = parent − Σ kids) negative — its layout then never
  // converges until a consistent tree lands. The previous *rendered* tree is
  // held whole instead, below (`mapTree`).
  // Under a filter an ancestor may stand in with its first-paint forest, when that forest carries the
  // next spine segment WITH children (an ancestor of match roots): a drilled deep link then paints
  // from the cheap reads instead of waiting on every ancestor's full forest.
  const spineHasKids = (t: TreeNode, i: number): boolean => {
    const seg = subtreePaths[i + 1]?.split('/').pop()
    return !!t.c?.some(k => k.n === seg && k.c?.length)
  }
  const dataFor = (i: number): TreeNode | null => {
    const full = subtreeQs[i]?.data?.tree
    if (full) return full
    const coarse = coarseQs[i]?.data?.tree ?? null
    if (i === subtreePaths.length - 1) return coarse
    return fq && coarse && spineHasKids(coarse, i) ? coarse : null
  }
  const baseTree: TreeNode | null = dataFor(0)
  // Whether this scan lists objects (a path-store generation, whose leaves
  // can be objects) or is a v1 dir-only index — every response of one scan
  // answers alike, so the first that has landed says.
  const objects = listsObjects([...subtreeQs, ...coarseQs].find(q => q.data?.tier)?.data?.tier)
  const rootErr = subtreeQs[0]?.error as Error | undefined
  // A drilled view the filter refuses (a heavy literal with no drilldown is `term-too-common` below the fleet
  // root): its reason, in the box's note and the map slot, as the root's would be — never the held parent's tiles.
  const deepErr = subtreePaths.length > 1 ? subtreeQs[subtreePaths.length - 1]?.error as Error | undefined : undefined
  const deepRefused = !rootErr && fq && refusalOf(deepErr) ? deepErr : undefined
  const viewErr = rootErr ?? deepRefused
  // The box's error: the client's own parse, else the server's 400.
  const fErr = fParse.error ?? fScopeRefused ?? /^400: bad query: (.*)/s.exec(viewErr?.message ?? '')?.[1]
  // useQueries returns a fresh array each render; stamp the data so the graft
  // memo re-runs exactly when a response lands.
  // Both tiers stamp the graft: a depth-1 tree landing must re-run it just
  // as a full one does.
  const subStamp = [...subtreeQs, ...coarseQs].map(q => q.dataUpdatedAt).join(',')
  const tree = useMemo((): TreeNode | null => {
    if (!baseTree) return null
    const graftAt = (t: TreeNode, segs: string[], sub: TreeNode, coarse: boolean): TreeNode => {
      const rec = (n: TreeNode, i: number): TreeNode => {
        // A depth-1 stand-in never replaces deeper children the parent's
        // tree already carries for this node: a drill into a tile the parent
        // drew to depth would otherwise flatten it to one level until the
        // full subtree lands (seconds, on a cold path).
        if (i === segs.length && coarse && n.c?.some(k => k.c?.length)) return n
        // Keep own totals; adopt the finer children — and the response root's
        // provenance (`pv`), which a parent-level view may have skipped.
        if (i === segs.length) return { ...n, c: sub.c, ...(sub.pv ? { pv: sub.pv } : {}) }
        const seg = segs[i]
        const kids = n.c ?? []
        if (kids.some(k => k.n === seg)) return { ...n, c: kids.map(k => (k.n === seg ? rec(k, i + 1) : k)) }
        // The spine segment fell below this level's pixel budget (it's inside
        // "(other)"): synthesize it from its own subtree response — the
        // response root carries the real totals — and shave those bytes off
        // the fold so the level still sums. Deeper segments wait for their
        // own level's graft to land.
        if (i !== segs.length - 1) return n
        const c = kids.map(k =>
          k.n === '(other)' ? { ...k, b: Math.max(0, k.b - sub.b), o: Math.max(0, k.o - sub.o) } : k)
        return { ...n, c: [...c, { ...sub, n: seg }] }
      }
      return rec(t, 0)
    }
    let t = baseTree
    subtreePaths.forEach((p, i) => {
      const sub = dataFor(i)
      if (p && sub) t = graftAt(t, p.split('/'), sub, !subtreeQs[i]?.data)
    })
    return t
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseTree, subtreePaths, subStamp])
  // Hold the last tree that rendered while the next one loads — whole, so it
  // is self-consistent (it already laid out fine). The map never flashes to
  // nothing across a scope, scan, or drill change, and the page below never
  // reflows; at worst `mapPath` truncates the new drill to an ancestor this
  // tree still has, until the new tree (depth-1 first, then full) replaces it.
  // Everything derived for the drawn map (`dateRange`, `catOrder`,
  // the rules section) follows `mapTree`, not `tree`, so a hold doesn't
  // empty the decorations under a map that is still showing.
  const lastTree = useRef<TreeNode | null>(null)
  if (tree) lastTree.current = tree
  // The live ownership ledger over the scan's attribution (`applyLedger`):
  // assignments recolor the map and its legend as soon as `/api/actions`
  // refetches. A pool or user-lens view also re-reads (`ledger` is in its
  // key) and comes back with the ledger folded in; a user lens is left as served.
  const heldTree = tree ?? lastTree.current
  const mapTree = useMemo(
    () => {
      if (!heldTree || !ownersMode || ownerMode === 'user') return heldTree
      // A pool is served with the ledger folded in (bytes assigned since the
      // scan move in or out of it); this only recolors by assignee.
      return applyLedger(heldTree, ownerIdx, store.scheme, canonId)
    },
    [heldTree, ownersMode, ownerMode, ownerIdx, store.scheme],
  )
  // What the map shows vs. what the page asked for: a held previous tree
  // (`mapStale` — dimmed, with a centered marker), or the asked-for tree with
  // a fetch still in flight (`mapBusy` — the full tree filling in behind the
  // depth-1 one, or a refresh; a corner marker, nothing dimmed).
  const mapStale = !tree && !!lastTree.current
  const mapBusy = mapStale || subtreeQs.some(q => q.isFetching)
  // The asked-for view failed (any non-2xx, its retries spent): the slot states it — a held tree from another
  // scan or scope would sit dimmed under "loading" forever (gcs 2026-10-09: 80 s of a gray map on a 500).
  const mapFailed = !!deepRefused || mapSlot(tree, lastTree.current, rootErr) === 'failed'
  const retryMap = () => { for (const q of [...subtreeQs, ...coarseQs]) if (q.isError) void q.refetch() }
  // A failed attempt being retried: say so on the held map's marker.
  const mapRetrying = subtreeQs[0]?.failureReason && !rootErr ? `retrying (${subtreeQs[0].failureReason.message.slice(0, 80)})…` : null

  // Bulk actions (the bar, the table's rows) under a filter take its matches under the view as the fewest
  // exact prefixes (`/api/filter-cover`): every match, listed, never a drawn subset or a row's whole prefix.
  // Unscoped only — under an owner or class scope a match's scoped bytes aren't a prefix.
  const canStageHere = store.staging && canStage
  const canAssignHere = ownersMode && canAssign
  const coverScoped = !!activeLens || ownerMode !== 'all' || !!classSet
  // Fetched on the viewer's intent (hovering or focusing the bulk bar, a row or its controls) and awaited
  // by a click, per scan, path and filter: a cold cover costs seconds, and most filtered page loads never act.
  // The page only observes the cache (`enabled: false`); `prefetchCover` / `fetchCover` fill it, deduped.
  const qc = useQueryClient()
  const coverArgs = useMemo(() => ({ date: asof ?? null, at: endAt, path: graftPath, q: fq, qs: syntax.id }), [asof, endAt, graftPath, fq, syntax.id])
  const coverKey = JSON.stringify([store.key, coverArgs])
  const coverQ = useFilterCover(sfetch, store.key, { ...coverArgs, enabled: false })
  const coverOk = !!fq && !!coverArgs.date && (canAssignHere || canStageHere)
  const coverIntent = useCallback(() => { if (coverOk && !coverScoped) void prefetchCover(qc, sfetch, store.key, coverArgs) }, [coverOk, coverScoped, qc, sfetch, store.key, coverArgs])
  const noScan = (): Promise<never> => Promise.reject(new HttpError('This scan isn’t loaded yet; try again in a moment.', 409))
  const coverResolve = useCallback(() => coverArgs.date ? fetchCover(qc, sfetch, store.key, coverArgs) : noScan(), [qc, sfetch, store.key, coverArgs])
  // A view whose answer doesn't list its matches — refused, a rollup or catalog answer, approximate — or has
  // none (the drilled root's matched total is empty, as the filter note's "no matches" says) offers no action
  // on them (`actBlock`): the bulk bar says why, muted, and no row offers one.
  const actBlocked = useMemo(() => {
    if (!fq) return null
    const d = subtreeQs[subtreeQs.length - 1]?.data ?? coarseQs[coarseQs.length - 1]?.data ?? subtreeQs[0]?.data
    const r = (d as { rollup?: { bucketsOnly?: true } } | undefined)?.rollup
    return actBlock({ refused: !!refusalOf(viewErr), rollup: !!r, bucketsOnly: !!r?.bucketsOnly, approximate: !!d?.approximateReason, dirsOnly: !!(d as { dirsOnly?: true } | undefined)?.dirsOnly, none: noMatches(dataFor(subtreePaths.length - 1)) })
  }, [fq, subStamp, viewErr]) // eslint-disable-line react-hooks/exhaustive-deps
  const tblFilter = useMemo((): RowSource | undefined => {
    if (!fq) return undefined
    // Under an owner or class scope a match's scoped bytes aren't a prefix: the click says so, muted.
    const scoped = (): Promise<never> => Promise.reject(new HttpError('Clear the owner or storage-class scope to act on the matches.', 409))
    return {
      key: `${coverKey}|${coverScoped}|${actBlocked ?? ''}`,
      blocked: actBlocked,
      resolve: row => coverScoped ? scoped() : !coverArgs.date ? noScan() : rowCover(qc, sfetch, store.key, coverArgs, row),
      prefetch: row => coverScoped || !coverOk ? Promise.resolve() : rowCover(qc, sfetch, store.key, coverArgs, row).catch(() => {}),
    }
  }, [fq, coverKey, coverScoped, coverOk, qc, sfetch, store.key, coverArgs, actBlocked]) // eslint-disable-line react-hooks/exhaustive-deps
  // The filter's match roots (the deepest subtree response carries them);
  // the series sums them per scan (the age chart follows the drill instead —
  // its own per-path index, below).
  // A heavy literal's view from its rollup (the server's `rollup`): its cells are children, not match
  // roots, and the series is the query's own sum per scan (`queryOnly`).
  const fRollup = useMemo(() => {
    if (!fq) return false
    const d = subtreeQs[subtreeQs.length - 1]?.data ?? coarseQs[coarseQs.length - 1]?.data ?? subtreeQs[0]?.data
    return !!(d as { rollup?: unknown } | undefined)?.rollup
  }, [fq, subStamp]) // eslint-disable-line react-hooks/exhaustive-deps
  const fSeries = useMemo(() => {
    if (!fq) return undefined
    // The first paint carries the same roots (the full read adds only what is inside them).
    const d = subtreeQs[subtreeQs.length - 1]?.data ?? coarseQs[coarseQs.length - 1]?.data ?? subtreeQs[0]?.data
    return seriesMatches(d)
  }, [fq, subStamp]) // eslint-disable-line react-hooks/exhaustive-deps
  // The same response's completeness: a budget-cut search (`partial`) or a
  // read without the search index (`approximate`) — shown beside the count.
  // The drilled view's own response root: its matched total under the drill path (the grafted `tree`'s root
  // is the fleet's).
  const fHere = fq ? dataFor(subtreePaths.length - 1) : null
  const fCoverage = useMemo(() => {
    if (!fq) return undefined
    const d = subtreeQs[subtreeQs.length - 1]?.data ?? coarseQs[coarseQs.length - 1]?.data ?? subtreeQs[0]?.data
    const r = (d as { rollup?: { bucketsOnly?: true; scopedBelow?: true } } | undefined)?.rollup
    return d && { partialReason: d.partialReason, approximateReason: d.approximateReason, bucketsOnly: r?.bucketsOnly ? (r.scopedBelow ? 'scoped' as const : true) : false, dirsOnly: !!(d as { dirsOnly?: true }).dirsOnly, hexRuns: d.hexRuns }
  }, [fq, subStamp]) // eslint-disable-line react-hooks/exhaustive-deps
  const meta: Meta | null = metaQ.data ?? null
  // Section `#hash` both ways (deep link in, scroll-spy out) and the scroll
  // position across navigation. A pending deep link is re-armed as the map,
  // meta and scans land (sections mount off different queries).
  const [spy] = useScrollSpyPref()
  useHashSpy({ ids: SECTION_IDS, location, navType, deps: [mapTree, meta, scans], legacy: LEGACY_ANCHORS, offset: topbarH, spy: spy === 'on' })
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { fmtBytes } = useUnits()
  // The treemap's drill path now lives in the URL *path* (below the store's own
  // route prefix), so a drilled prefix is a real shareable URL —
  // `/my-bucket/some/dir`, not `/?p=my-bucket/some/dir`. View
  // options stay query params (`?c`, `?mt`, …); the section stays in the `#hash`.
  const storeBase = store.path === '/' ? '' : store.path
  // A store home reads as `~` in the URL (`/~/c`); the path itself is the
  // expanded one (`Users/ryan/c`), each name travels encoded (`toUrlPath`), and a home URL spelled out is replaced by
  // its `~` form below.
  const urlPath = pathname.slice(storeBase.length).replace(/^\/+/, '')
  const drillPath = fromUrlPath(urlPath, store.home).join('/')
  const urlOf = (segs: string[]) => toUrlPath(segs, store.home)
  const canonUrlPath = urlOf(drillPath ? drillPath.split('/').filter(Boolean) : store.root ?? [])
  useEffect(() => {
    if (urlPath.replace(/\/+$/, '') !== canonUrlPath) navigate({ pathname: `${storeBase}/${canonUrlPath}`, search, hash: window.location.hash }, { replace: true })
  }, [urlPath, canonUrlPath]) // eslint-disable-line react-hooks/exhaustive-deps
  // The series' and the diff's path: at a filtered union root whose matches sit under one root (which the map
  // opens inside), that root — its member answers, where the union root refuses `q=` (`scopedPath`; undefined:
  // the root's answer isn't here yet).
  const scoped = { union: !!union, filtered: !!fq, tree: subtreeQs[0]?.data?.tree ?? coarseQs[0]?.data?.tree }
  const seriesPath = scopedPath(drillPath, scoped)
  const diffPathOf = scopedPath(graftPath, scoped)
  const diffPath = diffPathOf ?? graftPath
  const diffSegs = diffPath.split('/').filter(Boolean)
  // Per-path created-time strata for `AgeChart`, keyed on the drilled prefix so
  // it follows the drill exactly instead of showing the whole fleet at every
  // depth (specs/done/age-index.md). Root (`drillPath === ''`, depth 0) is the fleet
  // total; a prefix below the index floor returns no rows. Served by the pyrmts
  // pyramid (`/api/age-pyramid`, Phase B): the server picks the bin for the
  // budget and returns `{dt (epoch-ms), b, o}`; the chart still buckets to
  // day/week/month client-side, so we map `dt` back to an epoch-day `AgeRow`.
  // Bins to request from the pyramid: ~viewport-responsive (≈2 served atoms per
  // px of the chart, which is ~canW wide), so a phone fetches far fewer than a
  // desktop. The FE then re-buckets to the chosen day/week/month granularity.
  const ageBudget = Math.max(64, Math.min(1024, Math.round(canW / 2)))
  const ageQ = useQuery({
    queryKey: ['age', store.key, cutKey, drillPath, ageBudget],
    queryFn: async () => {
      const pf = perf.start('age', `${drillPath || '/'}@${asof}|b${ageBudget}`)
      const r = await pf.track(sfetch(`/api/age-pyramid?${cutQs}&path=${encodeURIComponent(drillPath)}&bin_budget=${ageBudget}`, { credentials: 'include', cache: 'no-cache' }))
      if (!r.ok) { pf.fail(); throw new Error(`age ${r.status}`) }
      const j = await r.json() as { records: { dt: number; b: number; o: number }[] }
      // No rows (no age tier for this deploy, or a prefix below the index
      // floor): the chart never mounts, so the load closes here.
      if (j.records.length) pf.decoded(); else pf.empty()
      return j
    },
    enabled: !!asof,
    staleTime: Infinity,
  })
  const age: AgeRow[] = useMemo(
    () => (ageQ.data?.records ?? []).map(r => ({ d: Math.floor(r.dt / 86400_000), b: r.b, o: r.o })),
    [ageQ.data],
  )
  // Same path, at the diff window's "before" scan — powers AgeChart's diff mode
  // (per-vintage grew/shrank). Only fetched when a diff window exists.
  const ageBaseQ = useQuery({
    queryKey: ['age', store.key, startAt ?? diffPrev, drillPath, ageBudget],
    queryFn: async () => {
      const pf = perf.start('age', `${drillPath || '/'}@${diffPrev}|b${ageBudget}`)
      const r = await pf.track(sfetch(`/api/age-pyramid?${startAt ? `at=${startAt}` : `date=${diffPrev}`}&path=${encodeURIComponent(drillPath)}&bin_budget=${ageBudget}`, { credentials: 'include', cache: 'no-cache' }))
      if (!r.ok) { pf.fail(); throw new Error(`age ${r.status}`) }
      const j = await r.json() as { records: { dt: number; b: number; o: number }[] }
      if (j.records.length) pf.decoded(); else pf.empty()
      return j
    },
    enabled: !!diffPrev,
    staleTime: Infinity,
  })
  const ageBase: AgeRow[] = useMemo(
    () => (ageBaseQ.data?.records ?? []).map(r => ({ d: Math.floor(r.dt / 86400_000), b: r.b, o: r.o })),
    [ageBaseQ.data],
  )
  // A drill leaves any opened object behind (`?open=` names a child of the
  // drilled directory), and the section hash: a new path starts at the top
  // (`useHashSpy`). Never copy the router's `hash` into a navigation — the spy
  // rewrites the URL's behind the router's back, so it's whatever section was
  // last *linked*, not the one in view (a crumb click warped to it).
  const drillTo = (segs: string[]) => {
    const q = new URLSearchParams(search)
    q.delete('open')
    navigate({ pathname: segs.length ? `${storeBase}/${urlOf(segs)}` : store.path, search: q.size ? `?${q}` : '' })
  }
  // `?open=<name>`: an object under the drilled directory, shown in the leaf
  // viewer below the map (objects.ts `openHref`). Opening pushes history, so
  // Back closes it.
  const [openP, setOpenP] = useUrlState('open', stringParam(), true)
  const openObject = (segs: string[]) => navigate(openHref(store.path, toUrlSegs(segs, store.home), search))
  // Read-recency lens domain: the access-log observation window (meta), not
  // the tree's own min/max — "no reads" is only meaningful vs when logging began.
  const readRange = useMemo((): DateRange | null =>
    meta?.access ? { min: meta.access.from, max: meta.access.to } : null,
  [meta])
  // No explicit `?c=` → a scope-appropriate default; an explicit pick always
  // wins: an attribution store colors by owner (on a one-owner view the
  // interesting axis is who else is in there), any other by tree, unless the
  // deployment selects a different default.
  const lensDefaultMode: ColorMode = storeDefaultColor(store)
  const setMode = (m: ColorMode) => setModeP(m === lensDefaultMode ? undefined : m)
  // The scan carries attribution (the owner axis and user coloring apply) —
  // from the scan's meta, not the current view, which may hold no user bytes
  // at all (e.g. `?o=unclaimed`).
  const hasAttr = !!meta?.users?.length
  // The page bar's controls, each on what backs it (`pageBar.ts`).
  const bar = barControls({ owners: store.owners, hasAttr, classes: store.prices, readRange: !!readRange })
  const effMode = mapColorMode(modeP, bar.color, lensDefaultMode)
  // The age chart's color axis: an explicit `?ac=` wins; otherwise it follows
  // the map. The read axis needs strata that carry `a` (scans published from
  // 8/29 on) — without them it's offered disabled and the chart falls back to
  // written.
  const ageReadRange = age.some(r => r.a != null) ? readRange : null
  // Only axes the rows actually carry a per-stratum value for are offered (no
  // dead buttons): `read` needs `a`, `user` needs `u`, `tree` needs `d1`. The
  // Phase-A per-path index carries only `(d, b, o)`, so `date` is the axis;
  // richer strata return with Phase B (specs/done/age-index.md).
  const ageModes = AGE_MODES.filter(m =>
    m === 'date'
    || (m === 'read' && !!ageReadRange)
    || (m === 'user' && hasAttr && age.some(r => r.u != null))
    || (m === 'tree' && age.some(r => r.d1 != null)))
  const ageMode: ColorMode = (() => {
    const want: ColorMode = ageModeP && (AGE_MODES as string[]).includes(ageModeP) ? (ageModeP as ColorMode) : effMode
    return ageModes.includes(want) ? want : 'date'
  })()
  // The pinned highlight the map dims to: the owner axis's user (a scoped
  // subtree still contains minority co-tenants, and user coloring should dim
  // them); the pools are sliced exactly, nothing to dim.
  const hl: Highlight | null = ownerUser ? { user: ownerUser } : null
  // Any scope narrower than "everything" — sections whose data can't follow
  // it (the age chart) hide rather than show fleet-wide numbers.
  const lensScoped = ownerMode !== 'all' || classSet != null
  // Diff sides: the drilled subtree at each endpoint, scoped like the map.
  // The diff is read server-side (`/api/diff`): both scans' index tiers at
  // one shared byte floor, point lookups for names that crossed it, the
  // page scope applied to both sides (specs/done/view-serving.md §2).
  // First paint: the bucket-level diff (`depth=1` — the two root reads plus
  // one level of lookups, ~2 s cold) stands in for the full walk while it
  // aligns, so the map shows the shape of the change before its detail.
  // Not under a path filter: its matches are found at every depth either way
  // (`depth` only caps what's drawn), so without a search index the "first"
  // paint costs as much as the full walk (gcs 2026-10-02: 9.3 s vs 4.5 s).
  const diffQ1 = useQuery<DiffData, Error>({
    queryKey: ['diff', store.key, diffKey, diffPath, canW, scopeKey, 'l1'],
    enabled: !!asof && !!diffPrev && scopeReady && diffPathOf !== undefined && !fq,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const pf = perf.start('dtm', viewKey(graftPath, `${diffPrev}→${asof}`, '|d1'), ['dtable'])
      const r = await pf.track(sfetch(
        `/api/diff?cv=${API_CV}&${diffQs}&path=${encodeURIComponent(diffPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}&depth=1`,
        { credentials: 'include', signal },
      ))
      if (!r.ok) { pf.fail(); throw apiError(r.status, await r.text()) }
      const j = await r.json() as DiffData
      // No rows: the section says "no changes" and the map never mounts.
      if (j.rows.length) pf.decoded(); else pf.empty()
      return j
    },
  })
  const diffL1 = diffQ1.data
  // The settled diff map's rendered height, held as the slot's floor while
  // the next pair loads under it (see the slot below).
  const diffSlotRef = useRef<HTMLDivElement>(null)
  const diffSlotH = useRef(0)
  const diffQ = useQuery<DiffData, Error>({
    queryKey: ['diff', store.key, diffKey, diffPath, canW, scopeKey],
    enabled: !!asof && !!diffPrev && scopeReady && diffPathOf !== undefined,
    // While the full walk aligns: the bucket-level diff of the SAME pair once
    // it lands, else the last pair's diff — drawn dimmed either way, so the
    // section holds its height and shows something before the detail.
    placeholderData: (prev: DiffData | undefined) => diffL1 ?? prev,
    staleTime: Infinity,
    refetchInterval: q => budgetCutRefetch(q.state.data, q.state.dataUpdateCount),
    retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
    retryDelay: (n: number) => 400 * 2 ** n,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const pf = perf.start('dtm', viewKey(graftPath, `${diffPrev}→${asof}`), ['dtable'])
      const r = await pf.track(sfetch(
        `/api/diff?cv=${API_CV}&${diffQs}&path=${encodeURIComponent(diffPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}`,
        { credentials: 'include', signal },
      ))
      if (!r.ok) { pf.fail(); throw apiError(r.status, await r.text()) }
      const j = await r.json() as DiffData
      // No rows: the section says "no changes" and the map never mounts.
      if (j.rows.length) pf.decoded(); else pf.empty()
      return j
    },
  })
  // The headline first: the same pair's totals without the row walk land in
  // a second or two, so the +X / Δobjects line shows while the rows align.
  const diffSumQ = useQuery<DiffData, Error>({
    queryKey: ['diff', store.key, diffKey, diffPath, canW, scopeKey, 'summary'],
    enabled: !!asof && !!diffPrev && scopeReady && diffPathOf !== undefined,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const r = await sfetch(
        `/api/diff?cv=${API_CV}&${diffQs}&path=${encodeURIComponent(diffPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}&summary=1`,
        { credentials: 'include', signal },
      )
      if (!r.ok) throw apiError(r.status, await r.text())
      return r.json() as Promise<DiffData>
    },
  })
  const diff: DiffData | null = diffQ.data ?? null
  const diffErr = diffQ.error
  // The shown diff is the previous pair's (placeholder) or the pair is still
  // aligning: its numbers describe another pair, so the subtitle says
  // "aligning" instead, and the drawn treemap dims under a marker.
  const diffStale = diffQ.isPlaceholderData || (!diff && diffQ.isFetching)
  // Two flavours of "still aligning": (1) refining — the map already shows
  // THIS pair's depth-1 diff (`diffL1`) while the full walk lands, so it's
  // correct as far as it goes; keep it bright and mark it with a small corner
  // pill (specs/done/treemap-first-class-everywhere.md §2 — no map-wide veil once
  // depth 1 has rendered). (2) genuinely stale — the map is still showing a
  // DIFFERENT pair's diff (the placeholder fell back to `prev`); that one is
  // misleading, so it dims under the centered marker until this pair lands.
  const diffRefining = diffQ.isPlaceholderData && !!diffL1 && diff === diffL1
  const diffStaleOther = diffStale && !diffRefining
  // Record the settled map's height after every commit that shows one; a
  // reload then holds that height under the marker instead of collapsing.
  useLayoutEffect(() => {
    if (!diffStale && diffSlotRef.current) diffSlotH.current = diffSlotRef.current.offsetHeight
  })
  // What the subtitle's numbers describe: the full diff once it's this pair's,
  // else the summary (its own query — current for this key or absent).
  const diffHead: DiffData | null = diff && !diffStale ? diff : diffSumQ.data ?? null
  // One-line description of the page scope, for the section subtitles:
  // where, then whose, then which names.
  // At the store root the scope is its buckets, counted (`2 buckets`) — the
  // path bar already says where the page is.
  const rootScope = mapTree?.c?.length ? `${mapTree.c.length} bucket${mapTree.c.length === 1 ? '' : 's'}` : store.rootLabel
  const scopeParts: string[] = [
    drillPath || rootScope,
    ...(ownerUser ? [`${shortName(ownerUser)}’s files${assigner ? `, assigned by ${shortName(assigner)}` : ''}`]
      : ownerMode === 'others' && notUsers[0] ? [`not ${shortName(notUsers[0])}`]
      : ownerMode !== 'all' ? [ownerMode] : []),
    ...(fq ? [`“${fq}”`] : []),
  ]
  const scopeDesc = scopeParts.join(' · ')
  // Diff model (built tree + movement totals + formatters), shared by the diff
  // header band and the diff map. Built here so the header stays mounted while a
  // diff is loading/errored (the map isn't rendered then).
  const diffModel = useDiffModel(diff, !diffPath, scopeDesc)
  // Controlled treemap drill path, resolved against the (possibly filtered/
  // scoped) tree each render: `?p=` survives scope toggles, filters, and scan
  // switches by re-walking the new tree; a vanished path truncates to its
  // deepest surviving ancestor.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!mapTree) return undefined
    const path = [mapTree]
    let cur: TreeNode = mapTree
    for (const s of drillPath.split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    // A store with one bucket (CoreWeave today) opens inside it — the bucket
    // level is a single full-width box otherwise.
    if (path.length === 1 && mapTree.c?.length === 1) return [mapTree, mapTree.c[0]]
    return path
  }, [mapTree, drillPath])
  // The table's path segments, stable while `mapPath` is (a fresh array per
  // render defeated every memo keyed on it).
  const tblSegs = useMemo(() => mapPath?.slice(1).map(n => n.n) ?? [], [mapPath])
  // Each table row's listed match roots: from the table's own path's response (full, else its first paint) —
  // exact when its `matched` list is every root, a lower bound when it is capped, unknown otherwise.
  const tblPath = mapPath ? tblSegs.join('/') : null
  const tblRowMatches = useMemo(() => {
    if (!fq || tblPath == null) return undefined
    const i = subtreePaths.indexOf(tblPath)
    const d = i < 0 ? undefined : (subtreeQs[i]?.data ?? coarseQs[i]?.data) as (MatchFields & { matchesCapped?: boolean; partialReason?: string; approximateReason?: string; rollup?: unknown }) | undefined
    return rowMatchesOf(d?.matched, matchedListOf(d))
  }, [fq, subStamp, tblPath, subtreePaths]) // eslint-disable-line react-hooks/exhaustive-deps
  const onMapPath = (p: TreeNode[]) => drillTo(p.slice(1).map(n => n.n))
  // Worklist rows / children table → drill the map to a prefix (the new path
  // starts at the top, where the map is).
  const openPath = (segs: string[]) => drillTo(segs)
  // A server user-lens map is already just that user's bytes — nothing to dim.
  const effHl: Highlight | null = activeLens ? null : hl

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])
  const mkUsers = useMemo(
    () => (meta?.users ?? []).map(u => u.u).sort((a, b) => shortName(a).localeCompare(shortName(b))),
    [meta],
  )

  // Legend-row pins land on the owner axis (a user, or the unowned pool).
  // `switchMode`: a ⌘K pick from any coloring jumps to an axis where the pick
  // is visible; a legend-row click is already on such an axis and must not
  // move it.
  const pickUser = (u: string, switchMode = true) => {
    setOwnerUser(u)
    if (switchMode && effMode !== 'user') setMode('user')
  }
  const pickUnowned = () => setOP('unowned')
  const clearHl = () => setOP(undefined)

  useActions({
    ...Object.fromEntries(
      // Only the axes this scan can color by (`bar.color`, the picker's own
      // list): a "Color by read" on a store with no access logs did nothing.
      // Bindings stay keyed to `MODES` order so a hotkey means one axis everywhere.
      MODES.flatMap((m, i) => bar.color.length > 1 && bar.color.includes(m) ? [[
        `mode:${m}`,
        {
          label: `Color by ${MODE_LABELS[m]}`,
          group: 'Color mode',
          keywords: MODE_KEYWORDS[m],
          defaultBindings: [String(i + 1)],
          handler: () => setMode(m),
        },
      ]] : []),
    ),
    'highlight:clear': {
      label: 'Clear the owner axis (everyone)',
      group: 'Scope',
      // `⇧x` belongs to row selection (toggle the page) in every table; the
      // owner axis has its own × button beside the picker.
      defaultBindings: ['alt+x'],
      handler: clearHl,
    },
    'nav:up': {
      label: 'Go up a directory level',
      group: 'Navigate',
      // Not Escape (that reads as "dismiss"; it stays for unpinning tips);
      // Backspace still pops the map too.
      defaultBindings: ['g u'],
      handler: () => { const s = drillPath.split('/').filter(Boolean); if (s.length) drillTo(s.slice(0, -1)) },
    },
    'owner:me': { label: 'Owner: my files', group: 'Scope', handler: () => setOP('me') },
    'owner:owned': { label: 'Owner: owned only', group: 'Scope', handler: () => setOP('owned') },
    'owner:unowned': { label: 'Owner: unowned only', group: 'Scope', handler: () => setOP('unowned') },
    'lens:classes': {
      label: 'Storage-class lens (hatch colder-class bytes)',
      group: 'View',
      defaultBindings: ['s'],
      handler: () => setLens(v => !v),
    },
    ...Object.fromEntries(
      (meta?.users ?? []).map(u => [
        `user:${u.u}`,
        {
          label: `${u.u} · ${fmtBytes(u.b)}`,
          group: 'Users',
          handler: () => pickUser(u.u),
        },
      ]),
    ),
    ...Object.fromEntries(
      scans.map(s => [
        `scan:${s}`,
        {
          label: `Scan ${fmtS(s)}`,
          group: 'Scans',
          handler: () => setDP(s),
        },
      ]),
    ),
  })

  // The written-date gradient's axis: the drawn leaves' dates — objects and
  // directories whose children this view didn't load alike (each is one cell)
  // — under the page's scale (`?ts=`; `rank` weighs each by its bytes). The
  // read lens keeps the access-log window as its domain, the same scale over it.
  const [dateScale] = useDateScale()
  const { dateRange, readAxis } = useMemo(() => {
    const ds: [number, number][] = []
    const as: [number, number][] = []
    const walk = (n: TreeNode) => {
      if (!n.c) {
        if (n.d != null) ds.push([n.d, n.b])
        if (n.a != null) as.push([n.a, n.b])
      }
      n.c?.forEach(walk)
    }
    if (mapTree) walk(mapTree)
    let min = Infinity
    let max = -Infinity
    for (const [d] of ds) {
      if (d < min) min = d
      if (d > max) max = d
    }
    return {
      dateRange: min < max ? dateAxis({ min, max }, dateScale, ds) : null,
      readAxis: readRange ? dateAxis(readRange, dateScale, as) : null,
    }
  }, [mapTree, dateScale, readRange])

  const catOrder = useMemo(() => {
    if (!mapTree) return []
    const catBytes = new Map<string, number>()
    for (const bucket of mapTree.c ?? [])
      for (const d of bucket.c ?? []) {
        const k = d.n.startsWith('(') ? '(other)' : d.n
        catBytes.set(k, (catBytes.get(k) ?? 0) + d.b)
      }
    return [...catBytes.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k).filter(k => k !== '(other)')
  }, [mapTree])

  // $ figures are GCS list prices per storage class, so they're only meaningful
  // for stores that have those classes — a CoreWeave bucket priced at GCS rates
  // would be an invented number, so its cost UI is dropped rather than faked.
  const estCost = useMemo(() => {
    if (!meta || !store.prices) return null
    const gib = (b: number) => b / 1024 ** 3
    const list = Object.entries(meta.class_bytes ?? {}).reduce(
      (s, [c, b]) => s + gib(b) * (CLASS_PRICE_US[c] ?? 0.02),
      0,
    )
    return { list }
  }, [meta, store])

  const pricing = useMemo((): Pricing | null => {
    if (!meta || !store.prices) return null
    const rates = (m?: Record<string, Record<string, number>>) =>
      m && Object.fromEntries(Object.entries(m).map(([k, cb]) => [k, ratePerByte(cb)]))
    return {
      blended: ratePerByte(meta.class_bytes),
      userRates: rates(meta.user_class_bytes),
      userMix: meta.user_class_bytes,
    }
  }, [meta, store])

  // Catch-all route: a first path segment that isn't one of the store's
  // buckets is a typo'd URL (/sweeps), not a drillable prefix — 404 it
  // instead of silently rendering the root view at a bogus address. Only an
  // unscoped root lists every bucket: under a filter or owner scope a real
  // bucket with nothing in scope is absent from it, and is "no matches", not
  // a 404. Display thresholds can also omit configured small buckets.
  const seg0 = drillPath.split('/')[0]
  if (!scopeQs && baseTree?.c && seg0 && !knownBucket(seg0, store, baseTree.c)) {
    return (
      <main>
        <SiteNav />
        <p className="err">
          404 — <code>/{drillPath}</code> is not a bucket or page here.{' '}
          <Link to={store.path}>home</Link>{store.staging && <> · <Link to="/staged">staged</Link></>}{store.owners && <> · <Link to="/users">users</Link></>}
        </p>
      </main>
    )
  }

  const segs = drillPath.split('/').filter(Boolean)
  const pathScheme = store.bucketSchemes?.[segs[0]] ?? store.scheme
  const capturedAt = meta?.captured_at ?? meta?.disk_space?.captured_at
  const recordedAt = capturedAt ?? meta?.published
  const scanTip = meta && (
    <div className="scan-tip">
      {capturedAt && (
        <div>captured {fmtTimestamp(capturedAt)}</div>
      )}
      {meta.published && (
        <div>published {fmtTimestamp(meta.published)}</div>
      )}
      <div><b>{fmtBytes(meta.total_bytes)}</b> · <b>{fmtN(meta.total_objects)}</b> objects across {store.rootLabel}</div>
      {estCost && (
        <div>
          est. <b>${Math.round(estCost.list).toLocaleString()}/mo</b> at list price
          <ClassMixTip mix={meta.class_bytes} note="GCS list prices (US regions) × scanned bytes; actual spend depends on the billing account's negotiated rates/credits" />
        </div>
      )}
    </div>
  )
  // One owner control (was two: an owned/unowned checklist + a person picker).
  // A single select — anyone / owned / unowned / a person — plus a `≠` toggle
  // that flips a picked person between their bytes and everyone-else's (the
  // negated `?o=!key` pool). `me` and any person the view already scopes to
  // stay selectable even if they haven't marked anything.
  const negated = ownerMode === 'others'
  const selPerson = ownerMode === 'user' ? (oP === 'me' ? 'me' : ownerUser)
    : negated ? (myUser && notUsers[0] === myUser ? 'me' : notUsers[0])
    : null
  const ownerSelVal = ownerMode === 'owned' ? 'owned' : ownerMode === 'unowned' ? 'unowned' : selPerson ?? ''
  const pickOwner = (v: string) =>
    v === '' ? setOP(undefined)
    : v === 'owned' || v === 'unowned' ? setOP(v)
    : v === 'me' ? setOP(negated ? (myUser ? `!${shortUserKey(myUser)}` : undefined) : 'me')
    : setOP(negated ? `!${shortUserKey(canonId(v))}` : shortUserKey(canonId(v)))
  const ownerSelect = (
    <>
      <FitSelect className="tb-select" value={ownerSelVal} ariaLabel="Owner"
        onChange={e => pickOwner(e.target.value)}>
        <option value="">anyone</option>
        <option value="owned">owned</option>
        <option value="unowned">unowned</option>
        {myUser && <option value="me">me ({shortName(myUser)})</option>}
        {mkUsers.filter(u => u !== myUser).map(u => <option key={u} value={u}>{shortName(u)}</option>)}
        {ownerUser && !mkUsers.includes(ownerUser) && ownerUser !== myUser && <option value={ownerUser}>{shortName(ownerUser)}</option>}
        {negated && notUsers[0] && notUsers[0] !== myUser && !mkUsers.includes(notUsers[0]) && <option value={notUsers[0]}>{shortName(notUsers[0])}</option>}
      </FitSelect>
      {selPerson && (
        <Explain text={negated
          ? <>Showing everyone <b>except</b> this person. Click for just theirs.</>
          : <>Invert: show everyone <b>else's</b> data under this view instead of this person's.</>}>
          <button type="button" className={`mini neg${negated ? ' on' : ''}`} aria-pressed={negated} onClick={() => negateOwner(!negated)}>not</button>
        </Explain>
      )}
      {ownerSelVal !== '' && (
        <Explain text="Clear the owner filter (back to anyone)">
          <button type="button" className="mini clear" aria-label="clear owner filter" onClick={() => setOP(undefined)}>×</button>
        </Explain>
      )}
    </>
  )
  const menu: MenuEntry[] = ownersMode && canAssign ? [{ key: 'typed', label: 'Assign a typed prefix…', onClick: () => setTypedOpen(true) }] : []
  // The bar's first row: where the page is. The map's own crumb strip is
  // hidden (app.scss) — this IS it, kept on screen mid-scroll; the deepest
  // node's totals ride along as the suffix.
  const here = mapPath?.[mapPath.length - 1]
  // The deepest crumb is a tap-to-open path card (the full `<scheme>…/` prefix +
  // copy) rather than another drill link — it's where the page already is.
  // A store home (`Users/ryan`) folds to one `~` crumb.
  // Where the store's own root is (`Store.root`), when this path is under it: crumbs above it would only bounce back.
  const storeRoot = store.root && store.root.every((r, i) => segs[i] === r) ? store.root : []
  const crumbFullPath = pathText(pathScheme, segs, store.home, true)
  const crumbCopy = pathCopy(pathScheme, segs, true)
  const disp = pathDisplay(pathScheme, segs, store.home)
  const crumbItems: { label: string; segs: string[] }[] = [
    ...(disp.lead === '~' ? [{ label: '~', segs: segs.slice(0, disp.leadSegs) }] : []),
    ...disp.rest.map((sg, i) => ({ label: sg, segs: segs.slice(0, disp.leadSegs + i + 1) })),
  ].filter(c => c.segs.length > storeRoot.length)  // the store root is the root crumb
  // The deepest crumb's page at the provider (`Store.externalLink`), as an icon right after its name.
  const atRoot = segs.length <= storeRoot.length
  const ext = <ExtLink link={store.externalLink?.(segs, here?.k !== 'file')} />
  const rootCrumb = <RootCrumb treeName={mapTree?.n} rootLabel={rootLabel} rootTitle={rootTitle} scopeWord={store.rootLabel} here={atRoot} onClick={() => drillTo(storeRoot)} />
  const crumbs = (
    <span className="tb-path" aria-label="Drilled path">
      {atRoot ? <span>{rootCrumb}{ext}</span> : rootCrumb}
      {crumbItems.map((c, i) => (
        <span key={i}>
          <span className="sep">/</span>
          {i === crumbItems.length - 1
            ? <><PathPopover label={c.label} fullPath={crumbFullPath} copy={crumbCopy} />{ext}</>
            : <Tooltip content={<code>{pathText(pathScheme, c.segs, store.home)}</code>}><button type="button" onClick={() => drillTo(c.segs)}>{c.label}</button></Tooltip>}
        </span>
      ))}
    </span>
  )

  return (
    <UnionMarks.Provider value={unionMarks}>
    <main>
      {typedOpen && <TypedPrefixModal idx={ownerIdx} onClose={() => setTypedOpen(false)} />}
      {/* The page scope, all of it, in the sticky bar — the same bar at the
          top of the page and mid-scroll, so every section reads against it:
          where (drill path) · when (scan, and the diff window's start while
          a section that shows it is on screen) · color axis · owner axis ·
          name filter. */}
      <SiteNav menu={menu} crumbs={crumbs}>
        {asof && scans.length > 1 && (
          <span className="tb-scan">
            {union
              ? <UnionScanPicker sources={union.sources} value={asof} at={at} scans={scans} onChange={setDP} onAt={setAt} label="Scan date" compact
                  fallback={<ScanCombobox value={asof} scans={scans} onChange={setDP} label="Scan date" fmt={fmtS} />} />
              : <ScanCombobox value={asof} scans={scans} onChange={setDP} label="Scan date" fmt={fmtS} />}
          </span>
        )}
        {/* How fresh the page's scan is, at a glance (the picker shows only
            its date); the tip carries the exact publish time + totals. */}
        {recordedAt && (
          <Tooltip content={scanTip}>
            <span className="tb-scan-ago" tabIndex={0}>scanned {ago(Date.parse(recordedAt) / 1000)} ago</span>
          </Tooltip>
        )}
        <LaptopIndicator />
        {bar.color.length > 1 && (
          <label className="tb-ctl">
            <span className="lbl">color</span>
            <Explain text={
              effMode === 'date' ? <>Object <b>{time.description}</b>, from the bucket listings (a folder’s cell = its newest object). {store.objectsNote}</>
              : effMode === 'read' ? <><b>Last observed read</b> — the most recent logged GET/HEAD/LIST anywhere under each cell (logging began {readRange ? epochDaysToDate(readRange.min) : '—'}). Brick-red = <b>no observed reads</b> since then. Logs may omit events; absence is not proof of no access.</>
              : effMode === 'user' ? <>Dominant <b>owner</b> of each cell; the legend lists the top users of the current view.</>
              : <>Top-level directory each cell belongs to.</>
            }>
              <FitSelect className="tb-select" value={effMode} ariaLabel="Color plots by" onChange={e => setMode(e.target.value as ColorMode)}>
                {bar.color.map(m => <option key={m} value={m}>{MODE_LABELS[m]}</option>)}
              </FitSelect>
            </Explain>
          </label>
        )}
        {bar.shade && (
          /* Secondary color axis: a shade *within* each cell's primary color.
             Opt-in (default none), so the primary axis reads as it always has. */
          <label className="tb-ctl">
            <span className="lbl">shade</span>
            <Explain text={<>A perturbation <i>within</i> each cell's color, on top of the primary axis. <b>storage class</b>: darker = a larger share of cold classes (Nearline / Coldline / Archive), so within one owner's band you can see what's already cold. Off by default.</>}>
              <FitSelect className="tb-select" value={shade} ariaLabel="Shade cells by" onChange={e => setSP(e.target.value === 'none' ? undefined : e.target.value)}>
                <option value="none">none</option>
                <option value="class">storage class</option>
              </FitSelect>
            </Explain>
          </label>
        )}
        {bar.classes && (
          <span className="tb-axis">
            <span className="lbl">class</span>
            <MultiSelect<ClassAxis>
              label="storage classes"
              options={CLASS_AXES.map(c => ({ key: c, label: CLASS_NAMES[CLASS_OF[c]], glyph: '●', color: CLASS_COLORS[CLASS_OF[c]], tip: `Only bytes in ${CLASS_NAMES[CLASS_OF[c]]} storage — every size on the page shrinks to that share (objects pro-rated).` }))}
              selected={classSet ? [...classSet] : CLASS_AXES}
              onChange={setClasses}
            />
          </span>
        )}
        {bar.ownerFilter && (
          <span className="tb-axis">
            <span className="lbl">owner</span>
            {ownerSelect}
          </span>
        )}
        {bar.pathFilter && (
          <span className={fq ? 'filterbox active' : 'filterbox'}>
            <Explain text={`Filter paths (${boxSyntax.describe().label}): ${boxSyntax.describe().summary} — the ? lists the forms.`}>
              <input
                value={fqDraft ?? fqRaw ?? ''}
                onChange={e => setFqDraft(e.target.value)}
                placeholder={boxSyntax.describe().placeholder}
                aria-label="Filter tree by path"
                data-log-input="filter"
                aria-invalid={!!fErr}
                size={30}
              />
            </Explain>
            {indexedOnly
              ? <QueryHelpTip syntaxes={[syntax]} active={syntax} />
              : <QueryHelpTip syntaxes={SYNTAXES} active={syntax} onPick={id => setQsP(id === storeSyntax.id ? undefined : id)} />}
            <FilterNote error={fErr} matched={fq && fHere ? matchedNote(fHere.b, !!graftPath, fmtBytes) : null} coverage={fCoverage}>
              {fq && !fErr && <UnionRefusals union={(subtreeQs[0]?.data ?? coarseQs[0]?.data)?.union} />}
              <Explain text="Clear the path filter"><button type="button" onClick={() => { setFqDraft(null); setFq(undefined) }}>✕</button></Explain>
            </FilterNote>
          </span>
        )}
        {fq && !coverScoped && (
          <BulkBar cover={coverQ.data} loading={coverQ.isFetching && !coverQ.data} onIntent={actBlocked ? undefined : coverIntent} resolve={coverResolve} resetKey={coverKey} blocked={actBlocked}
            scheme={store.scheme} query={fq} canAssign={canAssignHere} canStage={canStageHere} />
        )}
      </SiteNav>

      {/* Ambiguous `?d`: render the newest match (a best guess beats a dead
          end) with a strip listing every candidate to pin one. */}
      {asof && pending.length > 0 && <p className="tab-note pending-index" role="status">{pendingNote(asof, pending)}</p>}
      {union && <UnionMissingNote union={(subtreeQs[0]?.data ?? coarseQs[0]?.data)?.union} />}
      {dMatches.length > 1 && (
        <p className="disambig">
          <code>?d={encodeScan(dP) ?? dP}</code> matches {dMatches.length} scans — showing the newest; pin one:
          {dMatches.map(s => (
            <button key={s} className={s === asof ? 'on' : ''} onClick={() => setDP(s)}>{fmtS(s)}</button>
          ))}
        </p>
      )}
      {ownersQ.error && <p className="tab-note err">Assignments unavailable: {ownersQ.error.message}</p>}
      {meUnmapped && myUserReady && (
        <p className="tab-note">
          Your email isn't mapped to an owner id yet — ping Ryan (or an admin can add you at{' '}
          <code>/admin/db/user_emails</code>); pick any user from the owner menu to view their files.
        </p>
      )}
      {scansQ.isError && (
        <p className="tab-note" style={{ color: 'var(--s3)' }}>
          Couldn’t load snapshot data ({(scansQ.error as { status?: number })?.status === 401 ? 'not signed in — this dashboard is access-gated' : String(scansQ.error)}).
          {' '}<a href={signInUrl()}>Sign in</a> or reload once your session is active.
        </p>
      )}

      <DiskSpace space={meta?.disk_space} />
      {atRoot && <Allowances spec={store.allowances} classBytes={meta?.class_bytes} />}

      {mapTree && !mapFailed ? (
        <>
          {/* Remount per store: the treemap's caches are tied to the tree it
              mounted with, and a switch can swap `tree` without ever passing
              through null once both payloads are cached. */}
          {/* `leaf` folds the canvas away (height 0) on a v1 scan, whose index
              lists directories only: a drilled directory its own subtree fetch
              confirmed has nothing drawable (only objects, or dirs under the
              floor) — the note below says so. Never before that fetch answers:
              a childless dir may be a branch whose kids fell below the
              parent's pixel budget, and a zero-height canvas sends the core's
              squarify into a non-terminating loop on degenerate aspect ratios
              once they land. A scan that lists objects always has something
              to draw under a directory. */}
          <div id="tree-map" className={[
            'busy-host',
            !objects && mapPath && mapPath.length > 1 && !mapPath[mapPath.length - 1].c?.length && subtreeQs[subtreeQs.length - 1]?.data ? 'leaf' : '',
            mapStale ? 'stale' : '',
          ].filter(Boolean).join(' ')} aria-busy={mapBusy || undefined}><Treemap
            key={store.key}
            ownerLensed={ownerMode === 'user'}
            root={mapTree}
            mode={effMode}
            shade={shade}
            userIdx={userIdx}
            dateRange={dateRange}
            readRange={readAxis}
            hl={effHl}
            onPickUser={u => pickUser(u, false)}
            onPickUnowned={pickUnowned}
            onClearHl={clearHl}
            pricing={pricing}
            lens={lens}
            scheme={store.scheme}
            home={store.home}
            ownerIdx={ownersMode ? ownerIdx : undefined}
            path={mapPath}
            onPathChange={onMapPath}
            objects={objects}
            onOpen={p => openObject(p.slice(1).map(n => n.n))}
            brush={brush}
            onBrush={setBrush}
            band={band}
            onBand={setBand}
          />{mapStale ? <Busy label={mapRetrying ?? 'loading view…'} /> : mapBusy ? <Busy corner label="filling in…" /> : null}</div>
          {/* A drilled directory with nothing drawable under it: the scope came
              up empty, or (a v1 scan) it holds only objects or directories
              under this view's floor. Say so rather than show a blank canvas. */}
          {mapPath && mapPath.length > 1 && !mapPath[mapPath.length - 1].c?.length && subtreeQs[subtreeQs.length - 1]?.data && (
            mapPath[mapPath.length - 1].b === 0 && ownerMode !== 'all' ? (
              // The scope, not the directory, is what's empty here: say whose
              // filter came up dry rather than describe a 0-byte directory.
              <p className="hint leaf-note">
                {ownerMode === 'user' && ownerUser
                  ? <><b>{shortName(ownerUser)}</b> owns nothing under <code>{mapPath[mapPath.length - 1].n}</code> in this scan</>
                  : ownerMode === 'unowned'
                    ? <>Nothing under <code>{mapPath[mapPath.length - 1].n}</code> is unowned in this scan</>
                    : ownerMode === 'owned'
                      ? <>Nothing under <code>{mapPath[mapPath.length - 1].n}</code> is owned in this scan</>
                      : <>Nothing under <code>{mapPath[mapPath.length - 1].n}</code> is owned by anyone else in this scan</>}
                {' '}— widen the scope in the bar above, or press Backspace to go up.
              </p>
            ) : !objects && (
              <p className="hint leaf-note">
                <code>{mapPath[mapPath.length - 1].n}</code> holds {fmtN(mapPath[mapPath.length - 1].o)} objects and no directory of{' '}
                {fmtBytes(subtreeQs[subtreeQs.length - 1]!.data!.threshold ?? 0)} or more. This scan was indexed before objects were
                listed — pick a newer scan to see them, or press Backspace to go up.
              </p>
            )
          )}
          {/* The opened object (`?open=`), under the map it was picked from. */}
          {openP && mapPath && (
            <Suspense fallback={<p className="loading">loading the viewer…</p>}>
              <ObjectPanel
                segs={[...segs, openP]}
                node={mapPath.length - 1 === segs.length ? mapPath[mapPath.length - 1].c?.find(c => c.n === openP) : undefined}
                onClose={() => setOpenP(undefined)}
              />
            </Suspense>
          )}
          {/* The map's own listing — this node's children, or (`?tm=`) the largest files / folders anywhere below it:
              the whole directory's, so not under a scope (lens, owner pool, class, name filter) it couldn't honor. */}
          {mapPath && (
            <div id="tbl"><ChildrenTable
              node={mapPath[mapPath.length - 1]}
              segs={tblSegs}
              scheme={store.scheme}
              home={store.home}
              scan={asof}
              flatOk={objects && !scopeQs}
              ownerIdx={ownersMode ? ownerIdx : undefined}
              userIdx={userIdx}
              onPickUser={u => pickUser(u, false)}
              onOpen={openPath}
              onOpenObject={openObject}
              filter={tblFilter}
              rowMatches={tblRowMatches}
              brush={brush}
              onBrush={setBrush}
            /></div>
          )}
        </>
      ) : miss ? (
        <NoScanMatch miss={miss} fmt={fmtS} hrefFor={scan => hrefWithScan(pathname, search, selOf(new URLSearchParams(search)), scan, true, times, scans)} />
      ) : rootErr && /^(409|413)/.test(rootErr.message) ? (
        <p className="loading">
          {rootErr.message.startsWith('409') ? 'no per-user index for this scan — pick a newer scan, or clear the user'
            : 'this view is too wide for the index — drill in, or narrow the scope'}
        </p>
      ) : viewErr ? (
        // The filter's refusal as its reason, inline; any other failure as its message, a 5xx with a retry.
        <LoadFailure err={viewErr} what="view" onRetry={retryMap} />
      ) : noScansYet(scansQ) ? (
        // The list answered and is empty: no snapshot has index rows in D1,
        // so no view will ever load — a skeleton here would spin forever.
        // In dev this is the first-run state of `./dev --local-db` on an
        // unseeded local D1; say how to seed it.
        <p className="loading">
          No scans indexed yet — the scan list names only snapshots whose index rows are in D1 (<code>index_schema</code>), and there are none.
          {import.meta.env.DEV && <>{' '}Running <code>site/dev --local-db</code> on an empty local D1? Seed it from a prod export with <code>site/dev --refresh</code>.</>}
        </p>
      ) : (
        // First paint only — before even the depth-1 tree has landed (later
        // loads hold the previous tree instead): reserve the map's slot at its
        // fetch aspect (w : 0.6w), so nothing below it jumps when it arrives.
        <div id="tree-map" className="tm-skel" aria-busy="true" aria-label="loading tree" />
      )}

      {/* Bytes per scan under the drilled prefix, scoped like the map (a user
          or an owner pool) — one index row per scan via /api/series. The age
          chart still hides under any scope. */}
      <SizeOverTime
        scopeLabel={store.rootLabel}
        refused={fq && refusalOf(viewErr) ? viewErr : undefined}
        paths={fSeries?.paths}
        pathsTotal={fSeries?.pathsTotal}
        queryOnly={fRollup}
        filterLabel={fq ?? undefined}
        filterQs={fq ? `&q=${encodeURIComponent(fq)}&qs=${syntax.id}` : undefined}
        scans={scans} times={times} fmt={fmtS} prefix={seriesPath ?? drillPath}
        user={ownerUser}
        pool={ownerMode === 'unowned' ? 'unowned' : ownerMode === 'owned' ? 'owned' : null}
        ledgerRev={ledger}
        scopeReady={scopeReady && seriesPath !== undefined}
        onPickDate={setDP}
        onBrush={brushRange}
        window={diffWindow}
      />

      {startMiss && (
        <section id="diff">
          <NoScanMatch what="diff start" miss={startMiss} fmt={fmtS} hrefFor={scan => hrefWithScan(pathname, search, selOf(new URLSearchParams(search)), scan, false, times, scans)} />
        </section>
      )}
      {asof && diffPrev && (
        <section id="diff">
          <h2><SectionAnchor id="diff">Diff</SectionAnchor>{diff && (
            <Tooltip content={<>
              <b>{scopeDesc}</b> at each scan — the same scope as the map above (drill, lens, name filter), so in a lens
              a subtree that left the slice (e.g. got assigned to someone else) shows as shrunk even if its bytes didn’t move.
              Both scans are read at one byte floor ({fmtBytes(diff.threshold)}): a directory is named on both sides or folded into
              “(other)” on both, and one that crossed the floor is read exactly from the other scan — so every named cell’s Δ is real.
              {diff.lookups_capped && <> Some names drawn on one scan only went unread on the other (lookup budget): they’re left in “(other)” on both sides.</>}
              {diff.truncated && <> Largest changes shown — the diff walk was budget-capped, so the smallest movements aren’t enumerated (the totals are exact).</>}
            </>}><span className="info" tabIndex={0} aria-label="how this diff is read"> ⓘ</span></Tooltip>
          )}{diff && fq && (diff.partialReason || diff.approximateReason || diff.dirsOnly || diff.hexRuns) && (
            <span className="fflags"><FilterFlags partialReason={diff.partialReason} approximateReason={diff.approximateReason} dirsOnly={diff.dirsOnly} hexRuns={diff.hexRuns} /></span>
          )}</h2>
          {/* 2-row header band above the map: scan pickers + presets (with the
              status/error line) sit as `controls`, the colour legend beneath
              them; DiffHeader adds the movement table + area-mode toggle when the
              model is ready. Rendered here (not inside the map) so the pickers
              stay put while a diff is loading or errored. */}
          <DiffHeader model={diffModel} controls={<span className="sub">
            {/* Both endpoints: the window's start, and the page's scan again
                (the bar's picker — one scan, stated where the diff reads). */}
            <Explain text={<>The diff window's start — the size chart's shaded band reads from here to the scan. Drag on the size chart to set both ends.</>}>
              {union && diffPrev
                ? <UnionScanPicker sources={union.sources} value={diffPrev} at={atFrom} scans={earlier} onChange={setFrom} onAt={setFromAt} label="Diff from scan" before={at ?? (asof ? cutoffOfScan(union.sources, asof) : null)} compact
                    fallback={<ScanCombobox value={diffPrev} scans={earlier} onChange={setFrom} label="Diff from scan" fmt={fmtS} />} />
                : <ScanCombobox value={diffPrev} scans={earlier} onChange={setFrom} label="Diff from scan" fmt={fmtS} />}
            </Explain>
            <span className="arrow"> → </span>
            {union && asof
              ? <UnionScanPicker sources={union.sources} value={asof} at={at} scans={scans} onChange={setDP} onAt={setAt} label="Diff to scan (the page's scan)" compact
                  fallback={<ScanCombobox value={asof} scans={scans} onChange={setDP} label="Diff to scan (the page's scan)" fmt={fmtS} />} />
              : <ScanCombobox value={asof} scans={scans} onChange={setDP} label="Diff to scan (the page's scan)" fmt={fmtS} />}
            <span className="d-modes">
              <span className="gran d-mode" role="radiogroup" aria-label="Diff start">
                <span className="d-mode-lbl">start</span>
                <Explain text={<>Start tracks a <b>duration</b> back from the end (the buttons beside it): the window keeps its length as new scans arrive.</>}>
                  <button type="button" role="radio" aria-checked={!startPinned} className={startPinned ? '' : 'on'}
                    onClick={() => startPinned && setSpan(asof && diffPrev ? spanTo(asof, diffPrev) : undefined)}>duration</button>
                </Explain>
                <Explain text={<>Start is <b>pinned</b> to the scan picked on the left: the window's near end stays put as new scans arrive.</>}>
                  <button type="button" role="radio" aria-checked={startPinned} className={startPinned ? 'on' : ''}
                    onClick={() => !startPinned && setFrom(diffPrev ?? undefined)}>pinned</button>
                </Explain>
              </span>
              {!startPinned && spanPicks.length > 0 && (
                <span className="gran spans" role="radiogroup" aria-label="Diff span (back from the after scan)">
                  {spanPicks.map(({ label, ms, scan }) => (
                    <Explain key={label} text={<>Diff over the last {label}: {fmtS(scan)} → {fmtS(asof)}</>}>
                      <button role="radio" aria-checked={diffPrev === scan} className={diffPrev === scan ? 'on' : ''}
                        onClick={() => setSpan(scan === prevScan ? undefined : ms)}>
                        {label}
                      </button>
                    </Explain>
                  ))}
                </span>
              )}
              {endIsLatest && (
                <span className="gran d-mode" role="radiogroup" aria-label="Diff end">
                  <span className="d-mode-lbl">end</span>
                  <Explain text={<>End follows the <b>latest</b> scan as new ones arrive.</>}>
                    <button type="button" role="radio" aria-checked={!endPinned} className={endPinned ? '' : 'on'}
                      onClick={() => endPinned && setEndPin(false)}>latest</button>
                  </Explain>
                  <Explain text={<>End is <b>pinned</b> to the scan picked on the right.</>}>
                    <button type="button" role="radio" aria-checked={endPinned} className={endPinned ? 'on' : ''}
                      onClick={() => !endPinned && setEndPin(true)}>pinned</button>
                  </Explain>
                </span>
              )}
            </span>
            {diffHead ? (
              <>
                {diffStale && <span className="loading"> · aligning the rows…</span>}
              </>
            ) : diffErr && !diffStale && /^(404|409)/.test(diffErr.message) ? (
              <span className="tab-note">
                {' '}· {diffErr.message.startsWith('404')
                  ? <><code>{graftPath || '/'}</code> is in neither scan’s index — pick other scans or drill up.</>
                  : <>no per-user index for one of these scans — pick newer scans, or clear the user.</>}
              </span>
            ) : diffErr && !diffStale ? (
              // The filter's refusal as its reason; any other failure as its message, a 5xx with a retry.
              <>{' · '}<LoadFailure as="span" className="tab-note" err={diffErr} what={`diff ${fmtS(diffPrev)} → ${fmtS(asof)}`} onRetry={() => void diffQ.refetch()} /></>
            ) : (
              <span className="loading"> · aligning {fmtS(diffPrev)} → {fmtS(asof)}…</span>
            )}
          </span>} />
          <DiskSpaceDiff before={beforeMetaQ.data?.disk_space} after={meta?.disk_space} />
          {/* The slot keeps the treemap's height through a reload: the last
              diff dims under the marker, or (first load) a skeleton stands in.
              The height held is the one the last settled map actually drew
              (measured), not the request's canvas budget — the map is
              shorter than that, and a fixed floor left a blank band under it. */}
          {diff && diff.rows.length > 0 && diffModel && (
            <div ref={diffSlotRef} className={diffStaleOther ? 'diff-slot busy-host stale' : 'diff-slot busy-host'} style={diffStale && diffSlotH.current ? { minHeight: diffSlotH.current } : undefined}>
              {/* A drill in the diff drills the page: the map, the table and
                  the chart follow, and the diff itself re-reads at the new
                  prefix (its rows are relative to the drilled path). */}
              <DiffTreemap model={diffModel} onDrill={rel => drillTo([...diffSegs, ...rel])} onOpen={rel => openObject([...diffSegs, ...rel])} />
              {/* The map's tabular twin: the same cells as rows, sortable; a
                  row's name drills like its cell (and scrolls the maps up). */}
              <DiffTable model={diffModel} scheme={store.scheme} home={store.home} segs={diffSegs} onDrill={rel => openPath([...diffSegs, ...rel])} onOpen={rel => openObject([...diffSegs, ...rel])} />
              {diffStaleOther
                ? <Busy label={`aligning ${fmtS(diffPrev)} → ${fmtS(asof)}…`} />
                : diffRefining
                  ? <Busy label="aligning rows…" corner />
                  : null}
            </div>
          )}
          {diff && diff.rows.length === 0 && !diffStale && <p className="hint">No changes in this scope between the two scans.</p>}
          {!diff && diffStale && (
            <div className="diff-tm tm-skel busy-host stale" aria-busy="true"><Busy label={`aligning ${fmtS(diffPrev)} → ${fmtS(asof)}…`} /></div>
          )}
        </section>
      )}


      {/* The panels that don't follow the name filter hide under one (`filterBlind.ts`), one muted line in
          their place naming them. */}
      {(() => {
        const present: BlindPanel[] = [
          ...(!lensScoped && (ageQ.isPending || age.length > 0) ? ['age' as const] : []),
          ...(store.lifecycle ? ['lifecycle' as const] : []),
          ...(meta && store.prices ? ['classes' as const] : []),
          ...(hasAttr && mapTree ? ['ownership' as const] : []),
        ]
        const note = blindNote(!!fq, present)
        return note && <p className="loading filter-blind">{note}</p>
      })()}

      {/* Hidden when there's no age index for this deploy (e.g. r2 has no
          age-pyramid tier yet): show while loading or once rows arrive. */}
      {blindShown(!!fq) && !lensScoped && (ageQ.isPending || age.length > 0) && (
      <section id="mtime">
        {/* Granularity is auto-picked (and user-switchable) inside AgeChart, so
            the heading stays unit-free rather than lying about "month". */}
        <h2><SectionAnchor id="mtime">Bytes by {time.noun} date</SectionAnchor>{' '}
          <Tooltip content={<>
            The object’s <b>{time.description}</b> from the listing.
            {store.objectsNote}{readRange ? <>{' '}The other time axis is <b>last observed read</b> (from the usage logs, since {epochDaysToDate(readRange.min)}) —
            color by it to see which vintages have no recorded reads. Logs may omit events.</> : null}{' '}The chart’s color axis is its own (right):
            it follows the map’s until you pick one.
          </>}><span className="info" tabIndex={0} aria-label="about this chart">ⓘ</span></Tooltip>
        </h2>
        {ageQ.isPending && !!asof && <Skeleton height={220} label="loading ages…" />}
        {age.length > 0 && (
          <AgeChart rows={age} baseRows={diffPrev ? ageBase : undefined} diffLabels={diffPrev && asof ? { from: fmtS(diffPrev), to: fmtS(asof) } : undefined} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m)} modes={ageModes} userIdx={userIdx} readRange={ageReadRange} scheme={store.scheme} band={band} onBand={setBand} />
        )}
      </section>
      )}

      {/* Stores whose scan job snapshots the buckets' lifecycle rules get the
          fold here, last among the data sections; the rows diff against the previous scan. */}
      {blindShown(!!fq) && store.lifecycle && (
        <LifecycleFold
          store={store} asof={asof} prevScan={prevScan}
          note={<>Intended state is tracked in <code>{store.lifecycle.tracked}</code> (<code>dt-cloud lifecycle diff|push</code>).</>}
        />
      )}

      {blindShown(!!fq) && meta && store.prices && (() => {
        // Class mix of the *drilled* node (each node carries descendant-inclusive
        // `cb`), so this tracks the treemap instead of always showing fleet totals.
        const node = mapPath ? mapPath[mapPath.length - 1] : tree
        if (!node) return null
        const mix = classMix(node)
        const total = node.b || 1
        const scope = mapPath && mapPath.length > 1 ? mapPath.slice(1).map(n => n.n).join('/') : store.rootLabel
        return (
          <section id="storage-classes">
            <h2>Storage classes</h2>
            <p className="sub">
              Class mix + list-price estimate for <code>{scope}</code> — updates as you drill the treemap.
            </p>
            <table className="classes">
              <thead>
                <tr><th>class</th><th>bytes</th><th>est. $/mo (list, US)</th></tr>
              </thead>
              <tbody>
                {Object.entries(mix)
                  .sort((a, b) => b[1] - a[1])
                  .map(([c, b]) => (
                    <tr key={c}>
                      <td>
                        <Tooltip cell content={<>${CLASS_PRICE_US[c] ?? 0.02}/GiB·mo · {((100 * b) / total).toFixed(1)}% of these bytes</>}>
                          <span className="dotted">{CLASS_NAMES[c] ?? c}</span>
                        </Tooltip>
                      </td>
                      <td>{fmtBytes(b)}</td>
                      <td>${Math.round((b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02)).toLocaleString()}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </section>
        )
      })()}

      {/* Static attribution reference — how ownership is inferred + the rule tables.
          Reference material, so it sits last rather than sandwiched mid-page. */}
      {blindShown(!!fq) && hasAttr && mapTree && <AttributionRules tree={mapTree} />}

      <SiteKbd
        placeholder={store.owners ? 'Users, color modes, scans, pages…' : 'Color modes, scans, pages, actions…'}
        extra={[{ key: 'lens', label: `Class lens: ${lens ? 'on' : 'off'} (s)`, icon: <MdLayers />, onClick: () => setLens(v => !v) }]}
      />
    </main>
    </UnionMarks.Provider>
  )
}

export default function App() {
  return <AppContent />
}
