/** Bytes under a path per scan — the size-over-time chart, scoped like the
 * map (specs/done/view-serving.md §1 "series.json" → this).
 *
 *   GET /api/series?path=<P>[&lens=user:<id>|user:me][&o=owned|unowned]
 *
 * One point per scan the index knows: P's own row in that scan's coarsest
 * tier that holds it (or the floor-free tier), scoped per row like a view's
 * root. Nothing is precomputed per prefix and nothing is floored: a path
 * below every tier falls through to the floor-free tier. Scans predating the
 * index (or the scope's variant) have no per-prefix point — but for the
 * unscoped whole-bucket series (empty path, no lens / owner / class filter)
 * a scan's `meta.json` total is as good as an index row, so those scans are
 * filled in from the snapshot dir: the chart keeps any history that never
 * got (or lost) its tiers, rather than showing a gap.
 */
import { hiddenNotFound, isHidden, restrictionFor } from '../_lib/restrict.js'
import { type Ctx, json, requireScope, requireViewer } from '../_lib/auth.js'
import { snapshotsPrefix, traceJoins } from '../_lib/shared.js'
import { ivRetry, type Lens, pathGens, withPathStore, pathScans, servedStore, storeReady } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { classKey, ownerKey, ownerOk, type OwnerScope, parseClasses, parseOwner, QueryError, queryParam } from '../_lib/scope.js'
import { hexNote, hexQuery, liveTotal, rollupTotal, staticCompound, staticFilterStore, staticLiteral, staticTag } from '../_lib/staticFilter.js'
import { type BoolAnswer, boolPlan, compound } from '../_lib/staticBool.js'
import { indexedOnly, pathTerms, reject, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { poolLensFor, readRootAgg, readRootHistory, readRootRows } from '../_lib/view.js'
import type { FoldedLens } from '../_lib/owners.js'
import { type Hit, scanAt } from '../_lib/staticNames.js'
import { type OverTime, overTimePoint, readOverTime } from '../_lib/overTime.js'
import { parsePaths } from '../_lib/filter.js'
import { SERIES_MAX_PATHS } from '../_lib/seriesLimits.js'
import { metaRoots, rootPoints, type RootRow, type RootTrace } from '../_lib/series.js'
import { cutoffOf, latestCutoff, memberOf, memberRequest, membersAt, namesCutoff, openUnion, unionFor } from '../_lib/union.js'
import type { UnionView } from '../_lib/stores.js'
import { scanTimes } from '../_lib/scanTimes.js'
import { carryForward } from '../../src/federatedViews.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId, scanInstant } from '../../src/scanSlug.js'

// The default store's snapshot dirs (`snapshots/<date>/`; other stores live in
// a named subdir that `isScanId` keeps out), and the scan-id shape they're named by.

/** Scans present as snapshot dirs but absent from the index (oldest first). */
async function unindexedScans(env: Ctx['env'], indexed: Set<string>): Promise<string[]> {
  const store = servedStore(env, [snapshotsPrefix(env)])
  const out: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list(snapshotsPrefix(env), { cursor })
    for (const e of page.entries) {
      const d = e.key.slice(snapshotsPrefix(env).length).replace(/\/$/, '')
      if (e.isDir && isScanId(d) && !indexed.has(d)) out.push(d)
    }
    cursor = page.cursor
  } while (cursor)
  return out.sort()
}

type Meta = { total_bytes?: number; total_objects?: number; buckets?: Record<string, { total_bytes: number; total_objects: number }> }

async function readMeta(env: Ctx['env'], date: string): Promise<Meta | null> {
  try {
    const { bytes } = await servedStore(env, [snapshotsPrefix(env)]).get(`${snapshotsPrefix(env)}${date}/meta.json`)
    return JSON.parse(new TextDecoder().decode(bytes)) as Meta
  } catch (e) {
    console.log(`series /: no meta.json point for ${date}: ${(e as Error).message}`)
    return null
  }
}

/** A whole-bucket point from a scan's `meta.json` (no tiers needed). */
async function metaPoint(env: Ctx['env'], date: string): Promise<{ date: string; b: number; o: number } | null> {
  const m = await readMeta(env, date)
  return m && typeof m.total_bytes === 'number' ? { date, b: m.total_bytes, o: m.total_objects ?? 0 } : null
}

type SeriesCtx = Ctx & { waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: SeriesCtx): Promise<Response> => {
  // A union view (`STORES_JSON` `union`, `_lib/union.ts`): its members' series as one step function; a deployment
  // without one never leaves `seriesOf`.
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  return u ? unionSeries(ctx0, u) : seriesOf(ctx0)
}

/** The params a union root's series refuses: one store's axes (as `/api/subtree`'s root does). */
const UNION_ROOT_UNSCOPED = ['lens', 'o', 'cl', 'q', 'paths'] as const

/**
 * `/api/series` on a union view. The root: each member's own series (its scans, its points), concurrently, as
 * one step function over the union's events (`carryForward`): at each instant any member scanned, each member
 * contributes its latest point at or before it, and nothing before its first — a point some member doesn't
 * cover yet names it (`missing`), its total partial, never a zero. `split=roots`: every member's roots, each at
 * its member's carried point. Below the root: the path's member answers (its roots at the request's cutoff —
 * `date=`/`at=`/`d=`, as `/api/subtree` takes it — else at the latest).
 */
async function unionSeries(ctx0: SeriesCtx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return json({ error: 'bad path' }, 400)
  const un = await openUnion(ctx0, view, true)
  if (un instanceof Response) return un
  if (path !== '') {
    // The path's member at the page's cutoff when it names one (`date=`/`at=`/`d=`), else at the latest.
    const c = namesCutoff(url.searchParams) ? await cutoffOf(un, url.searchParams) : latestCutoff(un)
    if (c instanceof Response) return c
    const m = c && await memberOf(membersAt(un, c), path)
    if (!m) return json({ error: 'path not found' }, 404)
    return seriesOf({ ...ctx0, request: memberRequest(ctx0.request, m.store) })
  }
  const scoped = UNION_ROOT_UNSCOPED.filter(k => url.searchParams.get(k))
  if (scoped.length) return json({ error: `a union view's root is unscoped (${scoped.join(', ')}: drill into one store)` }, 400)
  const reads = await Promise.all(view.members.map(s => seriesOf({ ...ctx0, request: memberRequest(ctx0.request, s) })))
  const bad = reads.find(r => !r.ok)
  if (bad) return bad
  const bodies = await Promise.all(reads.map(r => r.json() as Promise<{ points: { date: string; b: number; o: number }[]; roots?: RootTrace[]; failed?: string[] }>))
  const traces = await Promise.all(bodies.map(async (body, i) => {
    const env = un.ctxs[i].env
    const times = await scanTimes(env, body.points.map(p => p.date))
    const rows = new Map<string, RootRow[]>()
    for (const t of body.roots ?? []) for (const p of t.points) {
      const r = rows.get(p.date) ?? []
      r.push({ path: t.path, b: p.b, o: p.o })
      rows.set(p.date, r)
    }
    return { source: view.members[i], points: body.points.map(p => ({ at: new Date(scanInstant(p.date, times)).toISOString(), value: { ...p, rows: rows.get(p.date) ?? [] } })) }
  }))
  const steps = carryForward(traces)
  const split = url.searchParams.get('split') === 'roots'
  const byDate = new Map<string, RootRow[]>()
  const points = steps.map(({ at, values }) => {
    // The step's scan id: the one a member observed at this instant (the first, in member order).
    const date = values.find((v, i) => v && traces[i].points.some(p => p.at === at && p.value === v))!.date
    const missing = view.members.filter((_, i) => !values[i])
    if (split) byDate.set(date, values.flatMap(v => v?.rows ?? []))
    return { date, b: values.reduce((n, v) => n + (v?.b ?? 0), 0), o: values.reduce((n, v) => n + (v?.o ?? 0), 0), ...(missing.length ? { missing } : {}) }
  })
  const failed = bodies.flatMap(b => b.failed ?? []).sort()
  const body = JSON.stringify({
    path, points,
    ...(split ? { roots: rootPoints(byDate) } : {}),
    ...(failed.length ? { failed } : {}),
    union: { view: view.key, sources: view.members.map((store, i) => ({ store, from: bodies[i].points[0]?.date ?? null })) },
  })
  return new Response(body, { headers: {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': failed.length ? 'no-store' : reads.map(r => r.headers.get('cache-control')).find(c => c === 'no-store') ?? reads[0]?.headers.get('cache-control') ?? 'no-store',
  } })
}

async function seriesOf(ctx0: SeriesCtx): Promise<Response> {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = await withPathStore(ctx1)
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  const gated = await st.time('auth', requireViewer(ctx))
  if (gated instanceof Response) return gated
  const url = new URL(request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return json({ error: 'bad path' }, 400)
  // `paths=` (specs/filter-views.md §4): the filter's match roots — the point
  // is the sum of one root read per scan. Bounded like a view's region reads.
  const paths = parsePaths(url.searchParams.getAll('paths'))
  if (paths.some(p => p.includes('..') || p.startsWith('/'))) return json({ error: 'bad paths' }, 400)
  if (paths.length > SERIES_MAX_PATHS) return json({ error: `too many paths (max ${SERIES_MAX_PATHS})` }, 400)
  // A path-restricted link (specs/path-scopes.md): hidden paths are absent.
  const restriction = restrictionFor(gated, env)
  if (restriction && [path, ...paths].some(p => isHidden(p, restriction))) return hiddenNotFound()
  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    if (env.STORE_KEY) return json({ error: LENS_PRIMARY_ONLY }, 400)
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return json({ error: 'bad lens (want user:<id>)' }, 400)
    // `user:me` → the caller's id: the shared cache key and the body carry the id.
    const resolved = await resolveLens(env, gated, { key: m[1] })
    if (!resolved) return json({ error: ME_UNRESOLVED }, 400)
    lens = resolved
  }
  const lensTag = lensParam(lens)
  const owner = parseOwner(url.searchParams.get('o'))
  const classes = parseClasses(url.searchParams.get('cl'))
  // `q=` (`qs=`): the map's filter. A single literal on a scan of the static name index's generation is
  // answered from it — Σ that scan's own match roots under `path` (`staticFilter.ts`); other scans and
  // other queries take `paths=`, the current scan's match roots, as before. A view root the query
  // matches is matched whole: its plain series.
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, env.QUERY_SYNTAX, indexedOnly(env))
  } catch (e) {
    // Indexed-only: a form it refuses is refused as such (`a b` is "one term", not "too short").
    const r = e instanceof QueryError && indexedOnly(env) ? rejectQuery(url.searchParams.get('q'), pathTerms(env)) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return json({ error: `bad query: ${e.message}` }, 400)
    throw e
  }
  // An indexed-only deployment: what the static index answers, no user lens or class scope (`_lib/indexedOnly.ts`).
  const strict = !!qp.query && indexedOnly(env)
  const refused = strict ? rejectQuery(url.searchParams.get('q'), pathTerms(env)) ?? rejectScope(!!(lens || classes)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })
  // The static index's hex-run rule: substrings match where they `occur` under it, static or not (`hexRuns.ts`).
  const hx = await hexQuery(env, qp.query)
  qp.query = hx.query
  const query = qp.query && !qp.query(path) ? qp.query : undefined
  if (qp.query && !query) paths.length = 0
  // A compound query (`a b`, `a|b`, `a -b`, `-b`; `staticBool.ts`): Σ each scan's match roots less its excluded
  // paths from the static answer — or, where the view root is the one match root (exclusions alone, an AND-group
  // its path holds), the root's own point less what it excludes.
  const bplan = qp.query && !lens && !classes ? boolPlan(qp.query.ast, pathTerms(env)) : null
  const sfs = (query || compound(bplan)) && !lens && !classes ? staticFilterStore(env) : null
  const skey = sfs && query ? staticLiteral(query.ast, pathTerms(env)) : null
  /** The query the response is keyed and answered by: a compound one the view root matches still subtracts its exclusions. */
  const keyed = query ?? (sfs && compound(bplan) && bplan.neg.length ? qp.query! : undefined)
  // `split=roots` (specs/done/root-geneses.md §1): the unscoped store root only —
  // one trace per depth-1 row (bucket) beside the total.
  const split = url.searchParams.get('split')
  if (split && split !== 'roots') return json({ error: 'bad split (want roots)' }, 400)
  if (split && (path || paths.length || lens || owner || classes || query)) return json({ error: 'split=roots is for the unscoped store root only' }, 400)

  // Every scan with a synced floor-free index, oldest first.
  const rows = await st.time('scans', pathScans(env, true))
  const dates = rows.results.map(r => r.date)
  // A user lens or an owner pool applies the live assignments, so its key carries the ledger head.
  const head = lens || owner && await hasLedger(env) ? await ledgerHead(env) : 0
  // Unscoped whole-bucket series only: scans without tiers still have a total in meta.json.
  const extra = path === '' && !paths.length && !query && !lens && !owner && !classes ? await st.time('unindexed', unindexedScans(env, new Set(dates))) : []
  // Two-tier cache (colo + KV, `_lib/edgeCache.ts`), keyed by every input
  // including the scan list and the ledger head, so an entry is immutable and
  // a new scan is a new key. This used to `cache.put` a `private` response
  // straight into the Workers Cache API, which refuses those (413) — so every
  // chart load re-read one point per scan (≈8 rounds of D1 + range reads for
  // a 94-scan history, 5–20 s) while the diff beside it was a cache hit.
  const g = await st.time('gens', pathGens(env, dates))
  const cacheKey = cacheKeyFor('series', cacheEnvTag(env, request), `${encodeURIComponent(path)}?P=${encodeURIComponent(paths.join(','))}&l=${lensTag}&o=${owner ? ownerKey(owner) : ''}&cl=${classKey(classes)}&s=${split ?? ''}&qs=${keyed ? qp.syntax : ''}&q=${encodeURIComponent(keyed ? url.searchParams.get('q') ?? '' : '')}&st=${staticTag(env, keyed)}&d=${dates.join(',')}&x=${extra.join(',')}&head=${head}&g=${g}&oh=${env.OWNER_HISTORY_BATCH === '1' ? 4 : 0}`, storeKey(env))
  const hit = await st.time('cache', cacheMatch(env, cacheKey))
  if (hit) return hit

  // Fast path (specs/done/obs-axis-indexing.md Phase 1): a path's series (or a
  // filter's, one line per match root) reads the cross-scan over-time index —
  // ⌈scans/K⌉ pruned reads per root — instead of one point read per scan.
  // Read only on a cache miss (it used to precede the check: ~1.2 s per hit).
  // `point()` below takes any scan the groups cover from here (absent there =
  // absent, the groups are floor-free); only the unsealed tip falls through to
  // the per-scan read. Lens / owner / class scopes and `split` aren't in the
  // index. Any line unreadable → all per-scan.
  // The static filter's match roots under `path`, or its rollup there (a heavy literal under a heavy
  // directory: the per-child running totals give every scan's total). Null: not a static literal, declined,
  // or a rollup under an owner pool (rollups carry no owners).
  const found = skey ? await st.time('static', sfs!.source.hits(skey, path)) : null
  const shits = found?.rollup && owner ? null : found
  const bgot = sfs && !skey && keyed && compound(bplan) ? await st.time('static', staticCompound(sfs, bplan, path, [])) : null
  const sbool: BoolAnswer | null = bgot && 'answer' in bgot ? bgot.answer : null
  // The scans it is exact on: the drilldown's are its base generation's, less the dir-only ones (a v1 source
  // lists no files, so a name's total there reads ~0 — a gap, not a point).
  const sscans = shits || sbool ? new Set((shits ?? sbool)!.scans ?? await sfs!.scans()) : null
  if (sscans) for (const d of await sfs!.dirOnly?.() ?? []) sscans.delete(d)
  // An anchored term with no anchors build is `anchor-not-indexed`, indexed-only or not; else, indexed-only, no
  // answer is `scan-not-indexed` (never the client's roots read per scan).
  const unanchored = query && skey && !found && sfs!.source.why?.(skey) === 'anchor-not-indexed'
  // A heavy literal's rollup under an owner pool (rollups carry no owners): `owner-too-common`.
  const ownerless = !!(found?.rollup && owner)
  if (unanchored || (keyed && strict && !shits && !sbool)) return new Response(rejectBody(bgot && 'reject' in bgot ? bgot.reject : reject(unanchored ? 'anchor-not-indexed' : ownerless ? 'owner-too-common' : 'scan-not-indexed')), { status: 400, headers: { 'content-type': 'application/json' } })
  if (query && !shits && !sbool && !paths.length) return json({ error: 'a filtered series needs its match roots (paths=)' }, 400)
  /** The scans a static answer doesn't cover: gaps in the series, named (indexed-only or not). */
  const unindexed: string[] = []
  const indexable = !split && !lens && !owner && !classes && !shits && !(sbool && !sbool.whole)
  const lines = indexable ? await st.time('overtime', Promise.all((paths.length ? paths : [path]).map(p => readOverTime(env, p)))) : []
  const ot: OverTime[] | null = lines.length && lines.every(Boolean) ? lines as OverTime[] : null
  let ownerHistory: Awaited<ReturnType<typeof readRootHistory>> = null
  if (env.OWNER_HISTORY_BATCH === '1' && !path && !paths.length && !query && !split && !classes && lens && !owner) {
    try {
      ownerHistory = await st.time('owner-history', ivRetry(() => readRootHistory(env, dates, { lens, owner })))
      st.trace('owner-history-points', ownerHistory?.size ?? 0, 'count')
    } catch (e) {
      const why = (e as Error).message
      st.trace('owner-history-fallback', 0, why)
      console.log(`owner history: canonical fallback: ${why}`)
    }
  }

  const points: { date: string; b: number; o: number }[] = []
  /** Scans whose point errored (not ones the path is absent from): named in the body, which isn't cached. */
  const failed: string[] = []
  // split=roots: each scan's depth-1 rows (the total is their sum), and for
  // tier-less scans whatever meta.json says about its roots.
  const rootsByDate = new Map<string, RootRow[]>()
  // One scan's point; a D1 hiccup ("internal error") gets one more try, and
  // anything else unreadable is a missing point, not a failed chart — logged,
  // since a silently absent point looks like a gap in the data.
  const point = async (date: string, tries = 2): Promise<{ date: string; b: number; o: number } | null> => {
    try {
      if (shits && sscans!.has(date)) {
        const t = shits.rollup ? rollupTotal(shits.rollup, date) : owner ? pooledTotal(shits.hits, date, owner, await poolLensFor(env, date, owner)) : liveTotal(shits.hits, date)
        return { date, b: t.b, o: t.o }
      }
      if (sbool && sscans!.has(date)) {
        if (sbool.none) return { date, b: 0, o: 0 }
        const pl = owner ? await poolLensFor(env, date, owner) : null
        const tot = (hits: Hit[]) => owner ? pooledTotal(hits, date, owner, pl) : liveTotal(hits, date)
        const r = tot(sbool.roots), e = tot(sbool.excl)
        if (!sbool.whole) return { date, b: r.b - e.b, o: r.o - e.o }
        const covered = ot ? overTimePoint(ot, date) : undefined
        const all = covered !== undefined ? covered : await ivRetry(() => readRootAgg(env, { date, path, lens, owner, classes }))
        return all ? { date, b: all.b - e.b, o: all.o - e.o } : null
      }
      // A scan the static answer doesn't cover: a gap, named — never the current scan's match roots read on
      // it (another scan's roots, or a rollup's few) nor a zero.
      if (shits || sbool) { unindexed.push(date); return null }
      if (ownerHistory?.has(date)) {
        const value = ownerHistory.get(date)
        return value ? { date, ...value } : null
      }
      const covered = ot ? overTimePoint(ot, date) : undefined
      if (covered !== undefined) return covered && { date, ...covered }
      if (split) {
        const rows = await ivRetry(() => readRootRows(env, date))
        if (!rows) return null
        rootsByDate.set(date, rows)
        return { date, b: rows.reduce((n, r) => n + r.b, 0), o: rows.reduce((n, r) => n + r.o, 0) }
      }
      if (paths.length) {
        // Σ over the match roots; a root absent from a scan contributes 0.
        const parts = await Promise.all(paths.map(p => ivRetry(() => readRootAgg(env, { date, path: p, lens, owner, classes }))))
        const b = parts.reduce((n, a) => n + (a?.b ?? 0), 0)
        const o = parts.reduce((n, a) => n + (a?.o ?? 0), 0)
        return parts.some(Boolean) ? { date, b, o } : null
      }
      const a = await ivRetry(() => readRootAgg(env, { date, path, lens, owner, classes }))
      return a ? { date, ...a } : null
    } catch (e) {
      const msg = (e as Error).message
      if (tries > 1 && /internal error/i.test(msg)) return point(date, tries - 1)
      console.log(`series ${path || '/'} ${lensTag || owner || ''}: no point for ${date}: ${msg}`)
      failed.push(date)
      return null
    }
  }
  // Scans are independent reads (each a few D1 round trips and one range
  // GET); a dozen at a time keeps a 40-scan history to ~4 rounds.
  for (let i = 0; i < dates.length; i += 12) {
    const got = await st.time('points', Promise.all(dates.slice(i, i + 12).map(d => point(d))))
    for (const g of got) if (g) points.push(g)
  }
  // A single-bucket store's tier-less history belongs to its sole root: the
  // earliest indexed scan having exactly one root says which.
  const first = [...rootsByDate.keys()].sort()[0]
  const soleRoot = first && rootsByDate.get(first)!.length === 1 ? rootsByDate.get(first)![0].path : null
  for (let i = 0; i < extra.length; i += 12) {
    const got = await Promise.all(extra.slice(i, i + 12).map(async d => {
      if (!split) return metaPoint(env, d)
      const m = await readMeta(env, d)
      if (!m || typeof m.total_bytes !== 'number') return null
      const roots = metaRoots(m, soleRoot)
      if (roots.length) rootsByDate.set(d, roots)
      return { date: d, b: m.total_bytes, o: m.total_objects ?? 0 }
    }))
    for (const g of got) if (g) points.push(g)
  }
  points.sort((a, b) => a.date.localeCompare(b.date))
  const body = JSON.stringify({ path, ...(qp.query ? hexNote(hx.hexRuns, qp.query.ast) : {}), ...(unindexed.length ? { unindexed: unindexed.sort() } : {}), ...(paths.length ? { paths } : {}), ...(lens ? { lens: lensTag } : {}), ...(owner ? { owner } : {}), points, ...(split ? { roots: rootPoints(rootsByDate) } : {}), ...(failed.length ? { failed: failed.sort() } : {}) })
  // A series missing points to errors is served (gaps beat no chart) but never cached: the next request retries them.
  if (failed.length) return new Response(body, { headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'server-timing': st.header() } })
  return cacheStore(env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx))
}

/** An owner pool's bytes and objects over static hits on `date` — the filtered view's numbers (`view.ts` `poolAgg` per
 *  match root): per path (a root, or an excluded path: none nests in another) its live slices' total and in-pool part,
 *  folded through the ledger's assignments since the scan (`pl`; null: none, the in-pool slices alone). */
export function pooledTotal(hits: Hit[], date: string, owner: OwnerScope, pl: FoldedLens | null): { b: number; o: number } {
  if (!pl) { const t = liveTotal(hits, date, u => ownerOk(u, owner)); return { b: t.b, o: t.o } }
  const D = scanAt(date)
  const per = new Map<string, [number, number, number, number]>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    const size = Number(h.size), n = Number(h.n)
    let t = per.get(h.path)
    if (!t) per.set(h.path, (t = [0, 0, 0, 0]))
    t[0] += size; t[1] += n
    if (ownerOk(h.usr === '' ? null : h.usr, owner)) { t[2] += size; t[3] += n }
  }
  let b = 0, o = 0
  for (const [p, [ab, ao, mb, mo]] of per) {
    b += Math.max(0, pl.value(p, ab, mb))
    o += Math.max(0, Math.round(pl.o.value(p, ao, mo)))
  }
  return { b, o }
}
