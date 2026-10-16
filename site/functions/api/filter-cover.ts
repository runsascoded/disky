/** The filter's matches under a path as the fewest exact prefixes (`_lib/cover.ts`): what the bulk bar and
 * the table's rows act on under a filter — never a row's whole prefix.
 *
 *   GET /api/filter-cover?date=<scan>|d=<slug>&path=<P>&q=<filter>[&qs=<syntax>]
 *
 * → `{date, path, q, items: [{path, kind, b, o, roots}], roots: {n, b, o}, complete, reason?, looked, unchecked}`
 *
 * `items` are sorted by path: a folder every object of which matches (`kind: 'dir'`, covering `roots` match
 * roots), or a match root itself (`dir`, `file`, or null when its kind couldn't be looked up — nothing acts
 * on those). Folders collapse at depth ≥ 2 only: a whole bucket is never a cover item unless its own name
 * matched. `complete: false` (with `reason`) when the search can't list every match here; the items are
 * then not offered for actions. Unscoped (no owner lens, pool or class): a scoped match isn't a prefix. */
import { type Env, requireViewer } from '../_lib/auth.js'
import { storeReady } from '../_lib/index.js'
import { queryParam, QueryError } from '../_lib/scope.js'
import { allMatchRoots, FILTER_VIEW_V, NotFound, pathTotals } from '../_lib/view.js'
import { COVER_V, coverFloor, coverSet, overCapReason } from '../_lib/cover.js'
import { hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, pathTerms, rejectBody, rejectQuery } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, keepFor, serverTiming } from '../_lib/edgeCache.js'
import { storeKey, type UnionView, withStore } from '../_lib/stores.js'
import { filteredVector, type Member, memberOf, memberRequest, type Refusal, refusalOf, refusalResponse, resolveUnion, UnionOverlap, unionFor } from '../_lib/union.js'
import { isScanId } from '../../src/scanSlug.js'
import { indexedScan, noScan, scanArg } from '../_lib/scanArg.js'
import { traceJoins } from '../_lib/shared.js'

/** The most items a response lists; past it, none (`complete: false`). A deployment's `COVER_ITEMS_MAX` env
 *  overrides it (tests). */
export const COVER_ITEMS_MAX = 50_000
const coverCap = (env: Env): number => Number((env as { COVER_ITEMS_MAX?: string }).COVER_ITEMS_MAX) || COVER_ITEMS_MAX

/** Row groups the over-cap bound's lookups (`coverFloor`) may read per call, and in all: a few shallow depth
 *  bands, not the cover's deepest-first walk (gcs 10-09 `nemotron`: 70,450 roots, 22 s cold to say "too many"). */
export const COVER_FLOOR_CALL_GROUPS = 16
export const COVER_FLOOR_GROUPS = 32

/** Row groups the one-object roots' kind lookups may read, all together (separate from the folders'). */
export const COVER_KIND_GROUPS = 64
/** Folders collapse at this depth or deeper (2: never a whole bucket). */
export const COVER_MIN_DEPTH = 2

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }
const jsonRes = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

export function onRequestGet(ctx0: Ctx): Promise<Response> {
  // A union view (`_lib/union.ts`): its members' covers side by side; a deployment without one never leaves `cover`.
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return Promise.resolve(u)
  return u ? unionCover(ctx0, u) : cover(ctx0)
}

type CoverItem = { path: string; kind: string | null; b: number; o: number; roots: number }
type CoverBody = { date: string; path: string; q: string; items: CoverItem[]; roots: { n: number; b: number; o: number }; complete: boolean; reason?: string; looked: number; unchecked: number }

/**
 * `/api/filter-cover` on a union view. The root: each ready member's own cover of its roots at its resolved scan
 * (its own static index; one without refuses for its mount), concatenated — items by path, `roots` / `looked` /
 * `unchecked` summed, complete only when every member's is and none refused (`reason` names each member's
 * `<store>: <why>`); `union` carries the vector, a refusing member's refusal beside its scan. Every member refusing:
 * the first refusal. Below the root: the path's member answers, at its resolved scan.
 */
async function unionCover(ctx0: Ctx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  try {
    const got = await resolveUnion(ctx0, view, true)
    if (got instanceof Response) return got
    const { resolved } = got
    const ask = (m: Member) => cover({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) })
    if (path !== '') {
      const m = await memberOf(resolved, path)
      return m ? ask(m) : new Response('path not found', { status: 404 })
    }
    const ready = resolved.members.filter(m => m.date !== null)
    const reads = await Promise.all(ready.map(ask))
    const refused: Refusal[] = []
    const parts: { store: string; body: CoverBody }[] = []
    for (const [i, r] of reads.entries()) {
      if (r.ok) { parts.push({ store: ready[i].store, body: await r.json() as CoverBody }); continue }
      const x = await refusalOf(ready[i].store, r)
      if (!x) return r
      refused.push(x)
    }
    if (!parts.length && refused.length) return refusalResponse(refused[0])
    const bs = parts.map(p => p.body)
    const reasons = [...parts.flatMap(p => (p.body.reason ? [`${p.store}: ${p.body.reason}`] : [])), ...refused.map(r => `${r.store}: ${r.error}`)]
    const sum = (f: (b: CoverBody) => number) => bs.reduce((n, b) => n + f(b), 0)
    return jsonRes({
      date: resolved.date, path, q: url.searchParams.get('q') ?? '',
      items: bs.flatMap(b => b.items).sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)),
      roots: { n: sum(b => b.roots.n), b: sum(b => b.roots.b), o: sum(b => b.roots.o) },
      complete: bs.every(b => b.complete) && !refused.length,
      ...(reasons.length ? { reason: reasons.join(' · ') } : {}),
      looked: sum(b => b.looked), unchecked: sum(b => b.unchecked),
      union: filteredVector(resolved, refused),
    }, 200)
  } catch (e) {
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`filter-cover failed: ${msg}`, { status: 500 })
  }
}

async function cover(ctx0: Ctx): Promise<Response> {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) return new Response('filter-cover API not configured (missing index store creds)', { status: 503 })
  const url = new URL(ctx.request.url)
  let date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!isScanId(date) && !url.searchParams.has('d')) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const qRaw = url.searchParams.get('q') ?? ''
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, ctx.env.QUERY_SYNTAX, indexedOnly(ctx.env))
  } catch (e) {
    const r = e instanceof QueryError && indexedOnly(ctx.env) ? rejectQuery(qRaw, pathTerms(ctx.env)) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  let query = qp.query
  if (!query) return jsonRes({ error: 'q= (a filter) is required' }, 400)
  const refused = indexedOnly(ctx.env) ? rejectQuery(qRaw, pathTerms(ctx.env)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })
  // An exclusion (`-x`) leaves holes inside its match roots: a root is then no exact prefix.
  if (query.neg) return jsonRes({ error: 'A filter with exclusions can’t be turned into whole folders or files to act on; drop the exclusion to act on the matches.', code: 'cover-exclusions' }, 400)

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  try {
    if (!url.searchParams.has('date')) {
      const scan = await scanArg(ctx.env, url.searchParams)
      if (scan instanceof Response) return scan
      date = scan
    }
    if (!await st.time('pre', indexedScan(ctx.env, date, true))) return noScan('date', date)
    // The static index's hex-run rule: the cover's roots are the filter's, matched as the map matches them.
    query = (await hexQuery(ctx.env, query)).query!
    if (indexedOnly(ctx.env)) {
      const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [date]))
      if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    const cacheKey = cacheKeyFor('filter-cover', cacheEnvTag(ctx.env, ctx.request), `${date}/${encodeURIComponent(path)}?qs=${qp.syntax}&q=${encodeURIComponent(qRaw)}&st=${staticTag(ctx.env, query)}&fv=${FILTER_VIEW_V}&cv=${COVER_V}`, storeKey(ctx.env))
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return hit

    const found = await st.time('roots', allMatchRoots(ctx.env, { date, path, query }))
    const roots = found?.roots ?? []
    const sum = { n: roots.length, b: roots.reduce((n, r) => n + r.b, 0), o: roots.reduce((n, r) => n + r.o, 0) }
    const why = found?.rollup ? 'There are too many matches under this folder to list them. Open a folder below to act on its matches.'
      : found?.coverage.partial?.length ? `Not every match here could be listed (${found.coverage.partial.join('; ')}).`
      : found?.coverage.approximate?.length ? `Small matches may be missing here (${found.coverage.approximate.join('; ')}).`
      : found?.coverage.dirsOnly?.length ? 'This scan lists folders only, so its file matches can’t be listed here. Pick a newer scan to act on the matches.'
      : null
    const cap = coverCap(ctx.env)
    // More roots than the cap: first a lower bound on the cover's size (`coverFloor`, a few shallow lookups —
    // the roots alone aren't one, full folders collapse them). Past the cap, the same refusal the cover would
    // give, without its walk; else the cover decides. At or under the cap by roots, the cover is too.
    const floor = !why && roots.length > cap
      ? await st.time('floor', coverFloor(roots, path, pathTotals(ctx.env, date, { call: COVER_FLOOR_CALL_GROUPS, total: COVER_FLOOR_GROUPS }), { minDepth: COVER_MIN_DEPTH, cap }))
      : null
    let body: Record<string, unknown>
    if (why || !roots.length) {
      body = { date, path, q: qRaw, items: [], roots: sum, complete: !why, ...(why ? { reason: why } : {}), looked: 0, unchecked: 0 }
    } else if (floor?.over) {
      body = { date, path, q: qRaw, items: [], roots: sum, complete: false, reason: overCapReason(floor.n, !floor.exact), looked: floor.looked, unchecked: 0 }
    } else {
      const cover = await st.time('cover', coverSet(roots, path, pathTotals(ctx.env, date), { minDepth: COVER_MIN_DEPTH, kinds: pathTotals(ctx.env, date, { call: 16, total: COVER_KIND_GROUPS }) }))
      const tooMany = cover.items.length > cap
      body = {
        date, path, q: qRaw, items: tooMany ? [] : cover.items, roots: sum, complete: !tooMany,
        ...(tooMany ? { reason: overCapReason(cover.items.length) } : {}),
        looked: cover.looked, unchecked: cover.unchecked,
      }
    }
    // A search its time budget cut is kept briefly (`keepFor`): the next ask recomputes it.
    return await cacheStore(ctx.env, cacheKey, JSON.stringify(body), { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keepFor({ searchLate: found?.coverage.searchLate }))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    const msg = String((e as Error).message ?? e)
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`filter-cover failed: ${msg}`, { status: 500 })
  }
}
