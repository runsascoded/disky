/** A name filter's match roots, largest first: the folders and files whose own names match (none above them does),
 * for the omnibar's "go to" results (`⌘K grug` → the `grug` folders, biggest first; specs/done/omnibar-folder-search.md).
 *
 *   GET /api/name-matches?date=<scan>|d=<slug>&q=<filter>[&qs=<syntax>][&path=<P>][&limit=<n>]
 *
 * → `{date, path, q, items: [{path, kind, b, o}], total, complete, reason?}`
 *
 * `items`: the `limit` (default 20, at most 100) largest match roots under `path` (default: the whole map), `kind`
 * `dir` / `file` (null when its lookup didn't fit the budget). `total`: every match root. `complete: false` (with
 * `reason`) when the search can't list every match (too many: open a folder, or filter the map instead). The roots
 * are the map's own (`allMatchRoots`: the static index's, sized from the path store), so a result's size is what the
 * map shows for it. */
import { type Env } from '../_lib/auth.js'
import { matchQuery, type MatchCtx } from '../_lib/matchQuery.js'
import { allMatchRoots, FILTER_VIEW_V, NotFound, pathTotals } from '../_lib/view.js'
import { staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, rejectBody } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, keepFor } from '../_lib/edgeCache.js'
import { storeKey, type UnionView } from '../_lib/stores.js'
import { filteredVector, type Member, memberOf, memberRequest, type Refusal, refusalOf, refusalResponse, resolveUnion, UnionOverlap, unionFor } from '../_lib/union.js'

/** Results per response: the default and the most `limit=` asks for. */
export const MATCHES_DEFAULT = 20
export const MATCHES_MAX = 100
/** Row groups the results' kind lookups may read, per call and together. */
export const MATCH_KIND_GROUPS = { call: 16, total: 32 }
/** Bump on a change to the response's shape (the edge cache keys on it). */
export const MATCHES_V = 1

type Item = { path: string; kind: 'dir' | 'file' | null; b: number; o: number }
type Body = { date: string; path: string; q: string; items: Item[]; total: number; complete: boolean; reason?: string }

const jsonRes = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const limitOf = (url: URL): number => Math.min(MATCHES_MAX, Math.max(1, Number(url.searchParams.get('limit')) || MATCHES_DEFAULT))
/** Largest first; ties by path. */
const bySize = <T extends { path: string; b: number }>(x: T, y: T) => y.b - x.b || (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)

export function onRequestGet(ctx0: MatchCtx): Promise<Response> {
  const u = unionFor(ctx0.request, ctx0.env as Env)
  if (u instanceof Response) return Promise.resolve(u)
  return u ? unionMatches(ctx0, u) : matches(ctx0)
}

/** On a union view: each ready member's matches at its resolved scan, merged largest first (`total` summed, complete
 *  only when every member's is and none refused); below the root, the path's member answers. */
async function unionMatches(ctx0: MatchCtx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  try {
    const got = await resolveUnion(ctx0 as never, view, true)
    if (got instanceof Response) return got
    const { resolved } = got
    const ask = (m: Member) => matches({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) })
    if (path !== '') {
      const m = await memberOf(resolved, path)
      return m ? ask(m) : new Response('path not found', { status: 404 })
    }
    const ready = resolved.members.filter(m => m.date !== null)
    const reads = await Promise.all(ready.map(ask))
    const refused: Refusal[] = []
    const parts: { store: string; body: Body }[] = []
    for (const [i, r] of reads.entries()) {
      if (r.ok) { parts.push({ store: ready[i].store, body: await r.json() as Body }); continue }
      const x = await refusalOf(ready[i].store, r)
      if (!x) return r
      refused.push(x)
    }
    if (!parts.length && refused.length) return refusalResponse(refused[0])
    const reasons = [...parts.flatMap(p => (p.body.reason ? [`${p.store}: ${p.body.reason}`] : [])), ...refused.map(r => `${r.store}: ${r.error}`)]
    return jsonRes({
      date: resolved.date, path, q: url.searchParams.get('q') ?? '',
      items: parts.flatMap(p => p.body.items).sort(bySize).slice(0, limitOf(url)),
      total: parts.reduce((n, p) => n + p.body.total, 0),
      complete: parts.every(p => p.body.complete) && !refused.length,
      ...(reasons.length ? { reason: reasons.join(' · ') } : {}),
      union: filteredVector(resolved, refused),
    }, 200)
  } catch (e) {
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`name-matches failed: ${msg}`, { status: 500 })
  }
}

async function matches(ctx0: MatchCtx): Promise<Response> {
  try {
    const m = await matchQuery(ctx0, 'name-matches')
    if (m instanceof Response) return m
    const { ctx, st, url, date, path, qRaw, qp, query } = m
    const limit = limitOf(url)
    const cacheKey = cacheKeyFor('name-matches', cacheEnvTag(ctx.env, ctx.request), `${date}/${encodeURIComponent(path)}?qs=${qp.syntax}&q=${encodeURIComponent(qRaw)}&n=${limit}&st=${staticTag(ctx.env, query)}&fv=${FILTER_VIEW_V}&mv=${MATCHES_V}`, storeKey(ctx.env))
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return hit

    const found = await st.time('roots', allMatchRoots(ctx.env, { date, path, query }))
    const roots = found?.roots ?? []
    const why = found?.rollup ? 'Too many matches to list: filter the map, or open a folder and search there.'
      : found?.coverage.partial?.length ? `Not every match could be listed (${found.coverage.partial.join('; ')}).`
      : found?.coverage.approximate?.length ? `Small matches may be missing (${found.coverage.approximate.join('; ')}).`
      : found?.coverage.dirsOnly?.length ? 'This scan lists folders only: file matches aren’t listed.'
      : null
    const top = roots.map(r => ({ path: r.path, b: r.b, o: r.o })).sort(bySize).slice(0, limit)
    const kinds = top.length ? await st.time('kinds', pathTotals(ctx.env, date, MATCH_KIND_GROUPS)(top.map(r => r.path))) : null
    const body: Body = {
      date, path, q: qRaw,
      items: top.map(r => ({ path: r.path, kind: kinds instanceof Map ? kinds.get(r.path)?.kind ?? null : null, b: r.b, o: r.o })),
      total: roots.length,
      complete: !why,
      ...(why ? { reason: why } : {}),
    }
    return await cacheStore(ctx.env, cacheKey, JSON.stringify(body), { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keepFor({ searchLate: found?.coverage.searchLate }))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    const msg = String((e as Error).message ?? e)
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`name-matches failed: ${msg}`, { status: 500 })
  }
}
