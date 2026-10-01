import { Explain } from './Help'
import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { MdLayers } from 'react-icons/md'
import { useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AGE_MODES, AgeChart } from './AgeChart'
import { canonId, shortName, shortUserKey } from './UserChip'
import { signInUrl, useCanAssign, useIdent as useIdentity } from './auth'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap, DiffHeader, useDiffModel } from './DiffTreemap'
import { DiffTable } from './DiffTable'
import type { DiffData } from './diffModel'
import { ScanCombobox } from './ScanCombobox'
import { ago, buildUserIndex, epochDaysToDate } from './colors'
import { ChildrenTable } from './ChildrenTable'
import { FitSelect } from './FitSelect'
import { PathPopover } from './PathPopover'
import { fromUrlSegs, pathCopy, pathDisplay, pathText, toUrlSegs } from './pathCrumbs'
import { listsObjects, openHref } from './objects'
import { Busy, Skeleton } from './Busy'
import { useRules } from './rules'
import { useHashSpy } from './hashSpy'
import { barControls } from './pageBar'
import { LifecycleFold } from './LifecycleFold'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight, ShadeMode } from './Treemap'
import { collectFlagged } from './filterTree'
import { BulkBar } from './BulkBar'
import { setCurrentScan, useMyUser, useOwnerIndex, useOwners } from './owners'
import { MultiSelect } from './MultiSelect'
import { SiteNav, topbarH } from './SiteNav'
import type { MenuEntry } from './SiteNav'
import { DAY, encodeScan, fmtScan, nearestScan, noScansYet, scanTime, useScan } from './scan'
import { SizeOverTime } from './SizeOverTime'
import { useStore, useStoreFetch } from './store'
import { perf } from './perf'
import { useDocTitle } from './title'
import { TypedPrefixModal } from './TypedPrefix'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_COLORS, CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, classMix, fmtN, fmtUsd, ratePerByte } from './types'
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
const MODE_CODES: Record<string, string> = { tree: 't', date: 'w', read: 'r', user: 'u' }
const MODE_ALIASES: Record<string, string> = {
  t: 'tree', tree: 'tree',
  w: 'date', written: 'date', age: 'date', date: 'date',
  r: 'read', read: 'read',
  u: 'user', user: 'user',
}
const modeCodec = {
  encode: (v: string | undefined) => (v === undefined ? undefined : MODE_CODES[v] ?? v),
  decode: (e: string | undefined) => (e === undefined ? undefined : MODE_ALIASES[e] ?? e),
}
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

// The owner axis: `?o=` is `owned`, `unowned`, `me`, or a user key
// (`?o=rw`); absent = everything. Owned = a person owns it (inferred from
// paths/runs, or assigned); unowned
// = the nobody-owns-it pool. A user narrows "owned" to that person.
type OwnerMode = 'all' | 'owned' | 'unowned' | 'user' | 'others'


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
  const { pathname, search, hash } = useLocation()
  const navigate = useNavigate()
  const store = useStore()
  const sfetch = useStoreFetch()
  // Legacy `?path=<prefix>` links (cw-s3 drilled by query param until the
  // union): forward to the URL-path form, keeping the other params.
  useEffect(() => {
    const q = new URLSearchParams(search)
    const legacy = q.get('path')
    if (legacy == null) return
    q.delete('path')
    const base = store.path === '/' ? '' : store.path
    const rest = q.toString()
    navigate({ pathname: legacy ? `${base}/${legacy.replace(/^\/+|\/+$/g, '')}` : store.path, search: rest ? `?${rest}` : '', hash }, { replace: true })
  }, [search, hash, navigate, store])
  const canAssign = useCanAssign()
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
  // and /user/:id via useScan (specs/scan-param-all-pages.md). Absent `?d` is
  // a first-class "latest", so a parked tab follows new scans.
  const { asof, scans, dMatches, dP, setDP, span, setSpan, from, setFrom, setEndPin, setRange, scansQ } = useScan(store)
  const rulesQ = useRules()
  const rules: Rules | null = rulesQ.data ?? null
  // Ledger actions record which scan the actor was viewing.
  useEffect(() => setCurrentScan(asof ?? undefined), [asof])
  const scanQuery = <T,>(name: string) => ({
    queryKey: [name, store.key, asof],
    queryFn: () => sfetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
  // `?f=` (name filter) and `?o=` (owner axis): the page's scope, sent to the
  // server with every view (see `scopeQs` below). The owner axis replaces the
  // old review *lenses* (`?l=user|unclaimed`, `?lu=`, and the `?u=` legend
  // pin). Old links normalize below.
  const [fq, setFq] = useUrlState('f', stringParam())
  // The box edits a local draft; the URL (and every query keyed on it) follows
  // after a 250 ms pause — one request pair per phrase, not per keystroke.
  const [fqDraft, setFqDraft] = useState<string | null>(null)
  useEffect(() => {
    if (fqDraft == null) return
    const t = setTimeout(() => { setFq(fqDraft || undefined); setFqDraft(null) }, 250)
    return () => clearTimeout(t)
  }, [fqDraft, setFq])
  // Lens changes push history (they change WHAT you're looking at, like a
  // drill); cosmetics (`?c=`, `?s=`, `?n=`) replace.
  const [oP, setOP] = useUrlState('o', stringParam(), true)
  // `?by=<assigner>` — the /assignments heatmap cell lens: with a user owner
  // lens, fold only the claims that assigner made. Only meaningful alongside a
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
  const myUser = useMyUser(ident?.email, ownersMode)
  // Owner axis. `me` resolves to the signed-in user's attribution id (a
  // shared `?o=me` link shows each reader their own files); an unmapped
  // email resolves to nothing, and the axis falls back to "all" with a note.
  // `?o=!<key>,<key>` — the "owned by someone OTHER than these people" pool
  // (the sweep console's "show me the conflicts under this band" link). The
  // excluded users resolve to canonical ids for the server's row filter.
  const notUsers: string[] =
    ownersMode && oP?.startsWith('!') ? oP.slice(1).split(',').filter(Boolean).map(k => canonId(k)) : []
  const ownerUser: string | null =
    !ownersMode || !oP || oP === 'owned' || oP === 'unowned' || oP.startsWith('!') ? null
    : oP === 'me' ? myUser
    : canonId(oP)
  const ownerMode: OwnerMode =
    !ownersMode || !oP ? 'all'
    : notUsers.length ? 'others'
    : oP === 'owned' ? 'owned' : oP === 'unowned' ? 'unowned' : ownerUser ? 'user' : 'all'
  const meUnmapped = ownersMode && oP === 'me' && !myUser
  const setOwnerUser = (u: string | undefined) => setOP(u === undefined ? undefined : u === 'me' ? 'me' : shortUserKey(canonId(u)))
  // Flip a picked person between their own bytes (`?o=<key>`) and everyone
  // else's under the current view (`?o=!<key>`) — the ≠ toggle beside the picker.
  const negateOwner = (on: boolean) => {
    const key = ownerUser ? shortUserKey(ownerUser) : notUsers.length ? shortUserKey(notUsers[0]) : null
    if (key) setOP(on ? `!${key}` : key)
  }
  const viewUser = ownerUser
  // Every scope axis is applied server-side by /api/subtree (specs/
  // view-serving.md §2): a user (`lens=user:`, the live claims folded in), a
  // pool (`o=`), the classes (`cl=`), the name filter (`q=`). The client
  // receives exactly the current view and only draws it.
  const lensUser = viewUser
  const activeLens = lensUser ? `user:${lensUser}` : null
  const assigner = ownersMode && byP ? canonId(byP) : null
  const scopeQs =
    (activeLens ? `&lens=${activeLens}` : '') +
    (activeLens && assigner ? `&by=${encodeURIComponent(assigner)}` : '') +
    (ownerMode === 'owned' || ownerMode === 'unowned' ? `&o=${ownerMode}` : '') +
    (notUsers.length ? `&o=!${notUsers.map(encodeURIComponent).join(',')}` : '') +
    (classSet ? `&cl=${CLASS_AXES.filter(c => classSet.has(c)).join('')}` : '') +
    (fq ? `&q=${encodeURIComponent(fq)}` : '')
  // One-time legacy-param rewrite onto the two axes, so old links (Slack
  // digests, /user pages) work and re-share in the current form:
  //   ?l=todo → ?k=u · ?l=unclaimed|communal, ?t=unattributed|communal → ?o=unclaimed
  //   ?l=user[&lu=x] (and older ?mt=mine[&mu=x]) → ?o=x|me · ?u=x (legend pin) → ?o=x
  //   any other ?t= (the retired group pin) → dropped
  useEffect(() => {
    const sp = new URLSearchParams(search)
    const legacy = ['l', 'lu', 'u', 'mt', 'mu', 't']
    const t = sp.get('t')
    // The owner pools were `claimed` / `unclaimed` until 2026-09-07.
    const oldPool = sp.get('o') === 'claimed' ? 'owned' : sp.get('o') === 'unclaimed' ? 'unowned' : null
    if (!legacy.some(k => sp.has(k)) && !oldPool) return
    if (oldPool) sp.set('o', oldPool)
    const l = sp.get('l') ?? sp.get('mt')
    const lu = sp.get('lu') ?? sp.get('mu')
    const u = sp.get('u')
    for (const k of legacy) sp.delete(k)
    if (t === 'unattributed' || t === 'communal') sp.set('o', 'unowned')
    if (l === 'todo') sp.set('k', 'u')
    else if (l === 'unclaimed' || l === 'communal') sp.set('o', 'unowned')
    else if (l === 'user' || l === 'mine') sp.set('o', lu ? shortUserKey(canonId(lu)) : 'me')
    if (u && !sp.has('o')) sp.set('o', shortUserKey(canonId(u)))
    navigate({ pathname, search: `?${sp.toString()}`, hash }, { replace: true })
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
  const earlier = useMemo(() => (asof ? scans.filter(s => s < asof) : []), [asof, scans])
  const spanScan = span && asof ? nearestScan(earlier, scanTime(asof) - span) : null
  // A pinned start (`from`) wins over a look-back span; both fall back to the
  // immediately-previous scan.
  const fromScan = from && asof ? nearestScan(earlier, scanTime(from)) : null
  const diffPrev = fromScan ?? spanScan ?? prevScan
  // Hour-rounded span back from `to` — the previous scan clears it, anything
  // else round-trips as its own span (nearest-scan resolution recovers it,
  // and the link keeps following `latest`).
  const spanTo = (to: string, from: string): number | undefined =>
    scans[scans.indexOf(to) + 1] === from
      ? undefined
      : Math.max(3600_000, Math.round((scanTime(to) - scanTime(from)) / 3600_000) * 3600_000)
  const pickBefore = (scan: string) => { if (asof) setSpan(spanTo(asof, scan)) }
  // A brush on the size chart hands back calendar dates; each resolves to the
  // scan on that date, and the pair becomes the page's `?d=` (after + span).
  const brushRange = (from: string, to: string) => {
    const toScan = scans.find(s => s.startsWith(to))
    const fromScan = scans.find(s => s.startsWith(from))
    if (!toScan || !fromScan || toScan <= fromScan) return
    setRange(toScan, spanTo(toScan, fromScan))
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
  // the span off, or already claimed by a shorter preset — are dropped
  // rather than mislabeled.
  const spanPicks = useMemo(() => {
    if (!asof) return []
    const t0 = scanTime(asof)
    const picks: { label: string; ms: number; scan: string }[] = []
    for (const [label, days] of SPANS) {
      const ms = days * DAY
      const best = nearestScan(earlier, t0 - ms)
      if (!best || Math.abs(scanTime(best) - (t0 - ms)) > ms / 4) continue
      if (!picks.some(p => p.scan === best)) picks.push({ label, ms, scan: best })
    }
    return picks
  }, [asof, earlier])
  // Lazy drill (specs/path-index-lazy-drill.md step 3, now the primary
  // source): the map's base is the pixel-budget subtree at the store root,
  // and every level of the drilled path gets its own subtree query, grafted
  // in depth order — interactive drills hit each level's cache as they go,
  // and a cold deep link fans the whole chain out in parallel.
  // The URL's `~` (a store home) expanded: `/~/c` → `Users/ryan/c`.
  const graftPath = fromUrlSegs(pathname.slice((store.path === '/' ? '' : store.path).length).split('/').filter(Boolean), store.home).join('/')
  const canW = Math.ceil((typeof window === 'undefined' ? 1280 : window.innerWidth) / 128) * 128
  // Perf-mark keys (`perf.ts`): what tells one load of a widget from another
  // on this page — path, scan(s), canvas width, scope.
  const viewKey = (p: string, d: string | null | undefined, extra = '') => `${p || '/'}@${d}|w${canW}${scopeQs}${extra}`
  const subtreePaths = useMemo(() => {
    const segs = graftPath.split('/').filter(Boolean)
    return ['', ...segs.map((_, i) => segs.slice(0, i + 1).join('/'))]
  }, [graftPath])
  const subtreeQs = useQueries({
    queries: subtreePaths.map(p => ({
      queryKey: ['subtree', store.key, asof, p, canW, scopeQs],
      enabled: !!asof,
      staleTime: Infinity,
      // Retry transient failures, but not the deterministic ones (409: no
      // user index for this scan; 413: view too wide) — those surface as-is.
      retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
      retryDelay: (n: number) => 400 * 2 ** n,
      // With a filter the full read plans each match root's tier (`full=1`);
      // the companion below paints the coarsest-tier forest first.
      queryFn: async ({ signal }: { signal?: AbortSignal }) => {
        const pf = perf.start('treemap', viewKey(p, asof, fq ? '|full' : ''), ['table'])
        const r = await pf.track(sfetch(
          `/api/subtree?cv=${API_CV}&date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}${fq ? '&full=1' : ''}`,
          { credentials: 'include', signal },
        ))
        if (!r.ok) { pf.fail(); throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`) }
        const j = await r.json() as { tree: TreeNode; tier?: string; matches?: string[]; matched?: { path: string; b: number; o: number }[]; threshold?: number }
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
      queryKey: ['subtree', store.key, asof, p, canW, scopeQs, 'depth1'],
      // Deepest path only — see `dataFor`; ancestors never use it.
      enabled: !!asof && i === subtreePaths.length - 1,
      staleTime: Infinity,
      retry: false,
      // Plain view: one depth band. Filtered view: the whole forest from the
      // coarsest tier (`partial`, milliseconds) — the fast first paint of
      // specs/filter-views.md, replaced by the planned-tier read above.
      queryFn: async ({ signal }: { signal?: AbortSignal }) => {
        const pf = perf.start('treemap', viewKey(p, asof, fq ? '|coarse' : '|d1'), ['table'])
        const r = await pf.track(sfetch(
          `/api/subtree?cv=${API_CV}&date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}${fq ? '' : '&depth=1'}${scopeQs}`,
          { credentials: 'include', signal },
        ))
        if (!r.ok) { pf.fail(); throw new Error(`${r.status}`) }
        const j = await r.json() as { tree: TreeNode; tier?: string }
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
  const dataFor = (i: number): TreeNode | null =>
    subtreeQs[i]?.data?.tree ?? (i === subtreePaths.length - 1 ? coarseQs[i]?.data?.tree ?? null : null)
  const baseTree: TreeNode | null = dataFor(0)
  // Whether this scan lists objects (a path-store generation, whose leaves
  // can be objects) or is a v1 dir-only index — every response of one scan
  // answers alike, so the first that has landed says.
  const objects = listsObjects([...subtreeQs, ...coarseQs].find(q => q.data?.tier)?.data?.tier)
  const rootErr = subtreeQs[0]?.error as Error | undefined
  // useQueries returns a fresh array each render; stamp the data so the graft
  // memo re-runs exactly when a response lands.
  // Both tiers stamp the graft: a depth-1 tree landing must re-run it just
  // as a full one does.
  const subStamp = [...subtreeQs, ...coarseQs].map(q => q.dataUpdatedAt).join(',')
  const tree = useMemo((): TreeNode | null => {
    if (!baseTree) return null
    const graftAt = (t: TreeNode, segs: string[], sub: TreeNode): TreeNode => {
      const rec = (n: TreeNode, i: number): TreeNode => {
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
      if (p && sub) t = graftAt(t, p.split('/'), sub)
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
  const mapTree = tree ?? lastTree.current
  // What the map shows vs. what the page asked for: a held previous tree
  // (`mapStale` — dimmed, with a centered marker), or the asked-for tree with
  // a fetch still in flight (`mapBusy` — the full tree filling in behind the
  // depth-1 one, or a refresh; a corner marker, nothing dimmed).
  const mapStale = !tree && !!lastTree.current
  const mapBusy = mapStale || subtreeQs.some(q => q.isFetching)

  // Bulk actions target the outermost matched prefixes — the nodes the server
  // flagged `m` (a match root's whole subtree comes along, so its descendants
  // aren't flagged).
  const fMatches = useMemo(() => (tree && fq ? collectFlagged(tree) : []), [tree, fq])
  // The filter's match roots (the deepest subtree response carries them);
  // the series sums them per scan (the age chart follows the drill instead —
  // its own per-path index, below).
  const matchedRoots = useMemo((): string[] | undefined => {
    if (!fq) return undefined
    const m = subtreeQs[subtreeQs.length - 1]?.data?.matched ?? subtreeQs[0]?.data?.matched
    return m?.map(x => x.path)
  }, [fq, subStamp]) // eslint-disable-line react-hooks/exhaustive-deps
  const meta: Meta | null = metaQ.data ?? null
  // Section `#hash` both ways (deep link in, scroll-spy out). Re-armed as the
  // map, meta and scans land (sections mount off different queries).
  useHashSpy({ ids: SECTION_IDS, hash, deps: [mapTree, meta, scans], legacy: LEGACY_ANCHORS, offset: topbarH })
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { fmtBytes } = useUnits()
  // The treemap's drill path now lives in the URL *path* (below the store's own
  // route prefix), so a drilled prefix is a real shareable URL —
  // `/marin-us-central1/ego-dex`, not `/?p=marin-us-central1/ego-dex`. View
  // options stay query params (`?c`, `?mt`, …); the section stays in the `#hash`.
  const storeBase = store.path === '/' ? '' : store.path
  // A store home reads as `~` in the URL (`/~/c`); the path itself is the
  // expanded one (`Users/ryan/c`), and a home URL spelled out is replaced by
  // its `~` form below.
  const urlPath = pathname.slice(storeBase.length).replace(/^\/+/, '')
  const drillPath = fromUrlSegs(urlPath.split('/').filter(Boolean), store.home).join('/')
  const urlOf = (segs: string[]) => toUrlSegs(segs, store.home).join('/')
  const canonUrlPath = urlOf(drillPath.split('/').filter(Boolean))
  useEffect(() => {
    if (urlPath.replace(/\/+$/, '') !== canonUrlPath) navigate({ pathname: `${storeBase}/${canonUrlPath}`, search, hash }, { replace: true })
  }, [urlPath, canonUrlPath]) // eslint-disable-line react-hooks/exhaustive-deps
  // Per-path created-time strata for `AgeChart`, keyed on the drilled prefix so
  // it follows the drill exactly instead of showing the whole fleet at every
  // depth (specs/age-index.md). Root (`drillPath === ''`, depth 0) is the fleet
  // total; a prefix below the index floor returns no rows. Served by the pyrmts
  // pyramid (`/api/age-pyramid`, Phase B): the server picks the bin for the
  // budget and returns `{dt (epoch-ms), b, o}`; the chart still buckets to
  // day/week/month client-side, so we map `dt` back to an epoch-day `AgeRow`.
  // Bins to request from the pyramid: ~viewport-responsive (≈2 served atoms per
  // px of the chart, which is ~canW wide), so a phone fetches far fewer than a
  // desktop. The FE then re-buckets to the chosen day/week/month granularity.
  const ageBudget = Math.max(64, Math.min(1024, Math.round(canW / 2)))
  const ageQ = useQuery({
    queryKey: ['age', store.key, asof, drillPath, ageBudget],
    queryFn: async () => {
      const pf = perf.start('age', `${drillPath || '/'}@${asof}|b${ageBudget}`)
      const r = await pf.track(sfetch(`/api/age-pyramid?date=${asof}&path=${encodeURIComponent(drillPath)}&bin_budget=${ageBudget}`, { credentials: 'include' }))
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
    queryKey: ['age', store.key, diffPrev, drillPath, ageBudget],
    queryFn: async () => {
      const pf = perf.start('age', `${drillPath || '/'}@${diffPrev}|b${ageBudget}`)
      const r = await pf.track(sfetch(`/api/age-pyramid?date=${diffPrev}&path=${encodeURIComponent(drillPath)}&bin_budget=${ageBudget}`, { credentials: 'include' }))
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
  // drilled directory).
  const drillTo = (segs: string[]) => {
    const q = new URLSearchParams(search)
    q.delete('open')
    navigate({ pathname: segs.length ? `${storeBase}/${urlOf(segs)}` : store.path, search: q.size ? `?${q}` : '', hash })
  }
  // `?open=<name>`: an object under the drilled directory, shown in the leaf
  // viewer below the map (objects.ts `openHref`). Opening pushes history, so
  // Back closes it.
  const [openP, setOpenP] = useUrlState('open', stringParam(), true)
  const openObject = (segs: string[]) => navigate({ ...openHref(store.path, toUrlSegs(segs, store.home), search), hash })
  // Read-recency lens domain: the access-log observation window (meta), not
  // the tree's own min/max — "no reads" is only meaningful vs when logging began.
  const readRange = useMemo((): DateRange | null =>
    meta?.access ? { min: meta.access.from, max: meta.access.to } : null,
  [meta])
  // No explicit `?c=` → a scope-appropriate default; an explicit pick always
  // wins: an attribution store colors by owner (on a one-owner view the
  // interesting axis is who else is in there), any other by tree.
  const lensDefaultMode: ColorMode = store.owners ? 'user' : 'tree'
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : lensDefaultMode
  const setMode = (m: ColorMode) => setModeP(m === lensDefaultMode ? undefined : m)
  // The scan carries attribution (the owner axis and user coloring apply) —
  // from the scan's meta, not the current view, which may hold no user bytes
  // at all (e.g. `?o=unclaimed`).
  const hasAttr = !!meta?.users?.length
  // The page bar's controls, each on what backs it (`pageBar.ts`).
  const bar = barControls({ owners: store.owners, hasAttr, classes: store.prices, readRange: !!readRange })
  const effMode: ColorMode = bar.color.includes(mode) ? mode : bar.color.includes('user') ? 'user' : 'tree'
  // The age chart's color axis: an explicit `?ac=` wins; otherwise it follows
  // the map. The read axis needs strata that carry `a` (scans published from
  // 8/29 on) — without them it's offered disabled and the chart falls back to
  // written.
  const ageReadRange = age.some(r => r.a != null) ? readRange : null
  // Only axes the rows actually carry a per-stratum value for are offered (no
  // dead buttons): `read` needs `a`, `user` needs `u`, `tree` needs `d1`. The
  // Phase-A per-path index carries only `(d, b, o)`, so `date` is the axis;
  // richer strata return with Phase B (specs/age-index.md).
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
  // page scope applied to both sides (specs/view-serving.md §2).
  // First paint: the bucket-level diff (`depth=1` — the two root reads plus
  // one level of lookups, ~2 s cold) stands in for the full walk while it
  // aligns, so the map shows the shape of the change before its detail.
  const diffQ1 = useQuery<DiffData, Error>({
    queryKey: ['diff', store.key, diffPrev, asof, graftPath, canW, scopeQs, 'l1'],
    enabled: !!asof && !!diffPrev,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const pf = perf.start('dtm', viewKey(graftPath, `${diffPrev}→${asof}`, '|d1'), ['dtable'])
      const r = await pf.track(sfetch(
        `/api/diff?cv=${API_CV}&from=${diffPrev}&to=${asof}&path=${encodeURIComponent(graftPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}&depth=1`,
        { credentials: 'include', signal },
      ))
      if (!r.ok) { pf.fail(); throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`) }
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
    queryKey: ['diff', store.key, diffPrev, asof, graftPath, canW, scopeQs],
    enabled: !!asof && !!diffPrev,
    // While the full walk aligns: the bucket-level diff of the SAME pair once
    // it lands, else the last pair's diff — drawn dimmed either way, so the
    // section holds its height and shows something before the detail.
    placeholderData: (prev: DiffData | undefined) => diffL1 ?? prev,
    staleTime: Infinity,
    retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
    retryDelay: (n: number) => 400 * 2 ** n,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const pf = perf.start('dtm', viewKey(graftPath, `${diffPrev}→${asof}`), ['dtable'])
      const r = await pf.track(sfetch(
        `/api/diff?cv=${API_CV}&from=${diffPrev}&to=${asof}&path=${encodeURIComponent(graftPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}`,
        { credentials: 'include', signal },
      ))
      if (!r.ok) { pf.fail(); throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`) }
      const j = await r.json() as DiffData
      // No rows: the section says "no changes" and the map never mounts.
      if (j.rows.length) pf.decoded(); else pf.empty()
      return j
    },
  })
  // The headline first: the same pair's totals without the row walk land in
  // a second or two, so the +X / Δobjects line shows while the rows align.
  const diffSumQ = useQuery<DiffData, Error>({
    queryKey: ['diff', store.key, diffPrev, asof, graftPath, canW, scopeQs, 'summary'],
    enabled: !!asof && !!diffPrev,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      const r = await sfetch(
        `/api/diff?cv=${API_CV}&from=${diffPrev}&to=${asof}&path=${encodeURIComponent(graftPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${scopeQs}&summary=1`,
        { credentials: 'include', signal },
      )
      if (!r.ok) throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`)
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
  // pill (specs/treemap-first-class-everywhere.md §2 — no map-wide veil once
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
  const diffModel = useDiffModel(diff, !drillPath, scopeDesc)
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
  const onMapPath = (p: TreeNode[]) => drillTo(p.slice(1).map(n => n.n))
  // Worklist rows / children table → drill the map to a prefix and show it.
  const openPath = (segs: string[]) => {
    drillTo(segs)
    document.querySelector('.dt-treemap')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  // A server user-lens map is already just that user's bytes — nothing to dim.
  const effHl: Highlight | null = activeLens ? null : hl

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])
  const mkUsers = useMemo(
    () => (meta?.users ?? []).map(u => u.u).sort((a, b) => shortName(a).localeCompare(shortName(b))),
    [meta],
  )

  // Legend-row pins land on the owner axis (a user, or the unclaimed pool).
  // `switchMode`: a ⌘K pick from any coloring jumps to an axis where the pick
  // is visible; a legend-row click is already on such an axis and must not
  // move it.
  const pickUser = (u: string, switchMode = true) => {
    setOwnerUser(u)
    if (switchMode && mode !== 'user') setMode('user')
  }
  const pickUnclaimed = () => setOP('unowned')
  const clearHl = () => setOP(undefined)

  useActions({
    ...Object.fromEntries(
      MODES.map((m, i) => [
        `mode:${m}`,
        {
          label: `Color by ${MODE_LABELS[m]}`,
          group: 'Color mode',
          defaultBindings: [String(i + 1)],
          handler: () => setMode(m),
        },
      ]),
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
    'owner:claimed': { label: 'Owner: owned only', group: 'Scope', handler: () => setOP('owned') },
    'owner:unclaimed': { label: 'Owner: unowned only', group: 'Scope', handler: () => setOP('unowned') },
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
          label: `Scan ${fmtScan(s)}`,
          group: 'Scans',
          handler: () => setDP(s),
        },
      ]),
    ),
  })

  // The written-date gradient's domain: the drawn leaves' dates — objects and
  // directories whose children this view didn't load alike (each is one cell).
  const dateRange = useMemo((): DateRange | null => {
    if (!mapTree) return null
    let min = Infinity
    let max = -Infinity
    const walk = (n: TreeNode) => {
      if (n.d != null && !n.c) {
        if (n.d < min) min = n.d
        if (n.d > max) max = n.d
      }
      n.c?.forEach(walk)
    }
    walk(mapTree)
    return min < max ? { min, max } : null
  }, [mapTree])

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
  // instead of silently rendering the root view at a bogus address.
  const seg0 = drillPath.split('/')[0]
  if (baseTree?.c && seg0 && !baseTree.c.some(k => k.n === seg0)) {
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
  const scanTip = meta && (
    <div className="scan-tip">
      {meta.published && (
        <div>published {new Date(meta.published).toISOString().replace('T', ' ').slice(0, 16)} UTC</div>
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
  const crumbFullPath = pathText(store.scheme, segs, store.home, true)
  const crumbCopy = pathCopy(store.scheme, segs, true)
  const disp = pathDisplay(store.scheme, segs, store.home)
  const crumbItems: { label: string; segs: string[] }[] = [
    ...(disp.lead === '~' ? [{ label: '~', segs: segs.slice(0, disp.leadSegs) }] : []),
    ...disp.rest.map((sg, i) => ({ label: sg, segs: segs.slice(0, disp.leadSegs + i + 1) })),
  ]
  const crumbs = (
    <span className="tb-path" aria-label="Drilled path">
      <Tooltip content={store.rootLabel}><button type="button" className={segs.length ? '' : 'here'} onClick={() => drillTo([])}>{mapTree?.n ?? store.rootLabel}</button></Tooltip>
      {crumbItems.map((c, i) => (
        <span key={i}>
          <span className="sep">/</span>
          {i === crumbItems.length - 1
            ? <PathPopover label={c.label} fullPath={crumbFullPath} copy={crumbCopy} />
            : <Tooltip content={<code>{pathText(store.scheme, c.segs, store.home)}</code>}><button type="button" onClick={() => drillTo(c.segs)}>{c.label}</button></Tooltip>}
        </span>
      ))}
    </span>
  )

  return (
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
            <ScanCombobox value={asof} scans={scans} onChange={setDP} label="Scan date" />
          </span>
        )}
        {/* How fresh the page's scan is, at a glance (the picker shows only
            its date); the tip carries the exact publish time + totals. */}
        {meta?.published && (
          <Tooltip content={scanTip}>
            <span className="tb-scan-ago" tabIndex={0}>scanned {ago(Date.parse(meta.published) / 1000)} ago</span>
          </Tooltip>
        )}
        {bar.color.length > 1 && (
          <label className="tb-ctl">
            <span className="lbl">color</span>
            <Explain text={
              effMode === 'date' ? <>Object <b>creation time</b>, from the bucket listings (each cell = the byte-weighted mean of its objects). {store.objectsNote}</>
              : effMode === 'read' ? <><b>Last read</b> — the most recent GET/HEAD/LIST anywhere under each cell, from the GCS usage logs (logging began {readRange ? epochDaysToDate(readRange.min) : '—'}). Brick-red = <b>never read</b> since then: prime deletion candidates.</>
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
          <span className="filterbox">
            <input
              value={fqDraft ?? fq ?? ''}
              onChange={e => setFqDraft(e.target.value)}
              placeholder="filter paths — text, a|b, or /regex/"
              aria-label="Filter tree by segment name"
              size={32}
            />
            {fq && tree && (
              <span className="fnote">
                {tree.b > 0 ? <>{fmtBytes(tree.b)} matched</> : 'no matches'}
                <Explain text="Clear the path filter"><button type="button" onClick={() => { setFqDraft(null); setFq(undefined) }}>✕</button></Explain>
              </span>
            )}
          </span>
        )}
        {fq && fMatches.length > 0 && (
          <BulkBar matches={fMatches} scheme={store.scheme} query={fq} />
        )}
      </SiteNav>

      {/* Ambiguous `?d`: render the newest match (a best guess beats a dead
          end) with a strip listing every candidate to pin one. */}
      {dMatches.length > 1 && (
        <p className="disambig">
          <code>?d={encodeScan(dP) ?? dP}</code> matches {dMatches.length} scans — showing the newest; pin one:
          {dMatches.map(s => (
            <button key={s} className={s === asof ? 'on' : ''} onClick={() => setDP(s)}>{fmtScan(s)}</button>
          ))}
        </p>
      )}
      {ownersQ.error && <p className="tab-note err">Assignments unavailable: {ownersQ.error.message}</p>}
      {meUnmapped && (
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

      {mapTree ? (
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
            readRange={readRange}
            hl={effHl}
            onPickUser={u => pickUser(u, false)}
            onPickUnclaimed={pickUnclaimed}
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
          />{mapStale ? <Busy label="loading view…" /> : mapBusy ? <Busy corner label="filling in…" /> : null}</div>
          {/* A drilled directory with nothing drawable under it: the scope came
              up empty, or (a v1 scan) it holds only objects or directories
              under this view's floor. Say so rather than show a blank canvas. */}
          {mapPath && mapPath.length > 1 && !mapPath[mapPath.length - 1].c?.length && subtreeQs[subtreeQs.length - 1]?.data && (
            mapPath[mapPath.length - 1].b === 0 && ownerMode !== 'all' ? (
              // The scope, not the directory, is what's empty here: say whose
              // filter came up dry rather than describe a 0-byte directory.
              <p className="hint leaf-note">
                {ownerMode === 'user' && lensUser
                  ? <><b>{shortName(lensUser)}</b> owns nothing under <code>{mapPath[mapPath.length - 1].n}</code> in this scan</>
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
          {/* The map's own listing — this node's children. */}
          {mapPath && (
            <div id="tbl"><ChildrenTable
              node={mapPath[mapPath.length - 1]}
              segs={tblSegs}
              scheme={store.scheme}
              home={store.home}
              ownerIdx={ownersMode ? ownerIdx : undefined}
              userIdx={userIdx}
              onPickUser={u => pickUser(u, false)}
              onOpen={openPath}
              onOpenObject={openObject}
            /></div>
          )}
        </>
      ) : rootErr ? (
        <p className="loading">
          {rootErr.message.startsWith('409') ? 'no per-user index for this scan — pick a newer scan, or clear the user'
            : rootErr.message.startsWith('413') ? 'this view is too wide for the index — drill in, or narrow the scope'
            : `view failed: ${rootErr.message}`}
        </p>
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
        paths={matchedRoots}
        filterLabel={fq ?? undefined}
        scans={scans} prefix={drillPath}
        user={ownerUser}
        pool={ownerMode === 'unowned' ? 'unowned' : ownerMode === 'owned' ? 'owned' : null}
        onPickDate={setDP}
        onBrush={brushRange}
        window={diffWindow}
      />

      {asof && diffPrev && (
        <section id="diff">
          <h2>Diff{diff && (
            <Tooltip content={<>
              <b>{scopeDesc}</b> at each scan — the same scope as the map above (drill, lens, name filter), so in a lens
              a subtree that left the slice (e.g. got assigned to someone else) shows as shrunk even if its bytes didn’t move.
              Both scans are read at one byte floor ({fmtBytes(diff.threshold)}): a directory is named on both sides or folded into
              “(other)” on both, and one that crossed the floor is read exactly from the other scan — so every named cell’s Δ is real.
              {diff.lookups_capped && <> Some small one-sided names went unread (lookup budget); they may sit in “(other)”.</>}
              {diff.truncated && <> Largest changes shown — the diff walk was budget-capped, so the smallest movements aren’t enumerated (the totals are exact).</>}
            </>}><span className="info" tabIndex={0} aria-label="how this diff is read"> ⓘ</span></Tooltip>
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
              <ScanCombobox value={diffPrev} scans={earlier} onChange={startPinned ? setFrom : pickBefore} label="Diff from scan" />
            </Explain>
            <Explain text={startPinned
              ? <>Start is <b>pinned</b> to this scan — the window's near end stays put as new scans arrive. Click to track a duration back from the end instead.</>
              : <>Start tracks a <b>duration</b> back from the end (the buttons). Click to pin it to this scan.</>}>
              <button type="button" role="switch" aria-checked={startPinned} className={'d-mode' + (startPinned ? ' on' : '')}
                onClick={() => startPinned
                  ? setSpan(asof && diffPrev ? spanTo(asof, diffPrev) : undefined)
                  : setFrom(diffPrev ?? undefined)}>
                {startPinned ? 'pinned' : 'duration'}
              </button>
            </Explain>
            <span className="arrow"> → </span>
            <ScanCombobox value={asof} scans={scans} onChange={setDP} label="Diff to scan (the page's scan)" />
            {endIsLatest && (
              <Explain text={endPinned
                ? <>End is <b>pinned</b> to this scan. Click to follow the latest scan as new ones arrive.</>
                : <>End follows the <b>latest</b> scan. Click to pin it to this one.</>}>
                <button type="button" role="switch" aria-checked={endPinned} className={'d-mode' + (endPinned ? ' on' : '')}
                  onClick={() => setEndPin(!endPinned)}>
                  {endPinned ? 'pinned' : 'latest'}
                </button>
              </Explain>
            )}
            {!startPinned && spanPicks.length > 0 && (
              <span className="gran spans" role="radiogroup" aria-label="Diff span (back from the after scan)">
                {spanPicks.map(({ label, ms, scan }) => (
                  <Explain key={label} text={<>Diff over the last {label}: {fmtScan(scan)} → {fmtScan(asof)}</>}>
                    <button role="radio" aria-checked={diffPrev === scan} className={diffPrev === scan ? 'on' : ''}
                      onClick={() => setSpan(scan === prevScan ? undefined : ms)}>
                      {label}
                    </button>
                  </Explain>
                ))}
              </span>
            )}
            {diffHead ? (
              <>
                {diffStale && <span className="loading"> · aligning the rows…</span>}
              </>
            ) : diffErr && !diffStale ? (
              <span className="tab-note">
                {' '}· {diffErr.message.startsWith('404')
                  ? <><code>{graftPath || '/'}</code> is in neither scan’s index — pick other scans or drill up.</>
                  : diffErr.message.startsWith('409')
                    ? <>no per-user index for one of these scans — pick newer scans, or clear the user.</>
                    : <>couldn’t diff {fmtScan(diffPrev)} → {fmtScan(asof)} ({diffErr.message}).</>}
                {' '}<button type="button" className="linkish" onClick={() => diffQ.refetch()}>retry</button>
              </span>
            ) : (
              <span className="loading"> · aligning {fmtScan(diffPrev)} → {fmtScan(asof)}…</span>
            )}
          </span>} />
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
              <DiffTreemap model={diffModel} onDrill={rel => drillTo([...segs, ...rel])} onOpen={rel => openObject([...segs, ...rel])} />
              {/* The map's tabular twin: the same cells as rows, sortable; a
                  row's name drills like its cell (and scrolls the maps up). */}
              <DiffTable model={diffModel} scheme={store.scheme} home={store.home} segs={segs} onDrill={rel => openPath([...segs, ...rel])} onOpen={rel => openObject([...segs, ...rel])} />
              {diffStaleOther
                ? <Busy label={`aligning ${fmtScan(diffPrev)} → ${fmtScan(asof)}…`} />
                : diffRefining
                  ? <Busy label="aligning rows…" corner />
                  : null}
            </div>
          )}
          {diff && diff.rows.length === 0 && !diffStale && <p className="hint">No changes in this scope between the two scans.</p>}
          {!diff && diffStale && (
            <div className="diff-tm tm-skel busy-host stale" aria-busy="true"><Busy label={`aligning ${fmtScan(diffPrev)} → ${fmtScan(asof)}…`} /></div>
          )}
        </section>
      )}


      {/* Hidden when there's no age index for this deploy (e.g. r2 has no
          age-pyramid tier yet): show while loading or once rows arrive. */}
      {!lensScoped && (ageQ.isPending || age.length > 0) && (
      <section id="mtime">
        {/* Granularity is auto-picked (and user-switchable) inside AgeChart, so
            the heading stays unit-free rather than lying about "month". */}
        <h2>Bytes by creation date{' '}
          <Tooltip content={<>
            When each stored byte was <b>written</b> — the object’s creation time from the listing.
            {store.objectsNote}{readRange ? <>{' '}The other time axis is <b>last read</b> (from the usage logs, since {epochDaysToDate(readRange.min)}) —
            color by it to see which vintages nobody has touched.</> : null}{' '}The chart’s color axis is its own (right):
            it follows the map’s until you pick one.
          </>}><span className="info" tabIndex={0} aria-label="about this chart">ⓘ</span></Tooltip>
        </h2>
        {ageQ.isPending && !!asof && <Skeleton height={220} label="loading ages…" />}
        {age.length > 0 && (
          <AgeChart rows={age} baseRows={diffPrev ? ageBase : undefined} diffLabels={diffPrev && asof ? { from: fmtScan(diffPrev), to: fmtScan(asof) } : undefined} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m)} modes={ageModes} userIdx={userIdx} readRange={ageReadRange} />
        )}
      </section>
      )}

      {/* Stores whose scan job snapshots the buckets' lifecycle rules get the
          fold here, last among the data sections; the rows diff against the previous scan. */}
      {store.lifecycle && (
        <LifecycleFold
          store={store} asof={asof} prevScan={prevScan}
          note={<>Intended state is tracked in <code>{store.lifecycle.tracked}</code> (<code>dt-cloud lifecycle diff|push</code>).</>}
        />
      )}

      {meta && store.prices && (() => {
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
                        <Tooltip placement="right" content={<>${CLASS_PRICE_US[c] ?? 0.02}/GiB·mo · {((100 * b) / total).toFixed(1)}% of these bytes</>}>
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
      {hasAttr && mapTree && <AttributionRules tree={mapTree} />}

      <SiteKbd
        placeholder="Users, color modes, scans, pages…"
        extra={[{ key: 'lens', label: `Class lens: ${lens ? 'on' : 'off'} (s)`, icon: <MdLayers />, onClick: () => setLens(v => !v) }]}
      />
    </main>
  )
}

export default function App() {
  return <AppContent />
}
