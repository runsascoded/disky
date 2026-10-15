/** Pixel-budget subtree of any path, served from the index tiers
 * (specs/done/view-serving.md; the folding lives in `_lib/view.ts`).
 *
 *   GET /api/subtree?date=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>]
 *                    [&lens=user:<id>|user:me][&o=owned|unowned][&q=<name filter>]
 *
 * The page's scope axes (specs/done/view-serving.md §2) are applied server-side:
 * the owner axis (`lens` for a user, `o` for the pools), the name filter
 * (`q`). Responses are immutable per (date, path, w₁₂₈, h₁₂₈, minArea,
 * atten, scope) plus the ledger head under a user lens — and cached in the
 * edge cache accordingly. w/h arrive quantized-up to 128px so resizes mostly
 * re-hit the cache.
 */
import { filterJson, hiddenNotFound, isHidden, restrictionFor, restrictSubtree } from '../_lib/restrict.js'
import { type Env, type Identity, requireViewer } from '../_lib/auth.js'
import { ivRetry, pathGens, withPathStore, storeReady, type Lens } from '../_lib/index.js'
import { parseOwner, queryParam, QueryError, classKey, parseClasses } from '../_lib/scope.js'
import { hasExtras } from '../_lib/extras.js'
import { ATTEN_DEFAULT, buildView, FILTER_VIEW_V, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT, readRootAgg, readRootRows, type ViewNode } from '../_lib/view.js'
import { hexNote, hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, isPartial, keepFor, serverTiming, UPGRADE_PHASE2_MS, upgradePartial } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId } from '../../src/scanSlug.js'
import { indexedScan, noScan, scanArg } from '../_lib/scanArg.js'
import { traceJoins } from '../_lib/shared.js'
import { type FilteredRoot, filteredVector, type Member, memberOf, memberRequest, type Refusal, refusalOf, refusalResponse, type Resolved, resolveUnion, UnionOverlap, unionFiltered, unionFor, unionRoot, vectorOf } from '../_lib/union.js'
import { filterThreshold } from '../_lib/filter.js'
import type { UnionView } from '../_lib/stores.js'


type SubtreeCtx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = (ctx0: SubtreeCtx): Promise<Response> => {
  // A union view (`STORES_JSON` `union`, `_lib/union.ts`): its members' roots side by side; a deployment
  // without one never leaves `subtree`.
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return Promise.resolve(u)
  return u ? unionSubtree(ctx0, u) : subtree(ctx0, true)
}

/** Fill the edge cache for a subtree URL without a caller identity: what a
 * share card's unfurl does so the clickthrough's first paint is warm
 * (`og/serve.ts`). The response never leaves the Worker. */
export const warmSubtree = (env: Env, url: string, waitUntil?: (p: Promise<unknown>) => void): Promise<Response> =>
  subtree({ request: new Request(url), env, waitUntil }, false)

/** A union member's read (`unionSubtree`): the identity its gate already resolved, and the union root's
 * common byte threshold (one fleet-wide budget, never each member's own). */
interface MemberRead {
  id: Identity | null
  threshold?: number
}

async function subtree(ctx0: SubtreeCtx, gate: boolean, member?: MemberRead): Promise<Response> {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = withPathStore(ctx1)
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) {
    return new Response('subtree API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  let date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  if (!isScanId(date) && !url.searchParams.has('d')) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

  // Optional lens: `lens=user:<id>` — a treemap of that user's bytes, read
  // from the by-user sort. `user:me` resolves to the caller below, once the
  // gate has identified them.
  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    if (ctx.env.STORE_KEY) return new Response(LENS_PRIMARY_ONLY, { status: 400 })
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return new Response('bad lens (want user:<id>)', { status: 400 })
    lens = { key: m[1] }
  }

  const rawOwner = url.searchParams.get('o')
  const owner = parseOwner(rawOwner)
  const by = url.searchParams.get('by') ?? undefined
  // `depth=N`: cap the read N levels below the root (see ViewOpts.maxDepth).
  const depth = Number(url.searchParams.get('depth')) || undefined
  const classes = parseClasses(url.searchParams.get('cl'))
  const qRaw = url.searchParams.get('q') ?? ''
  const full = url.searchParams.get('full') === '1'
  // `q=` in `qs=`'s syntax: a parse error (or an unknown syntax) is a 400
  // whose text the filter box shows under itself.
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, ctx.env.QUERY_SYNTAX)
  } catch (e) {
    // Indexed-only: a form it refuses is refused as such (`a b` is "one term", not "too short").
    const r = e instanceof QueryError && indexedOnly(ctx.env) ? rejectQuery(url.searchParams.get('q'), url.searchParams.get('qs'), ctx.env.QUERY_SYNTAX) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  let query = qp.query
  // An indexed-only deployment: one literal, unscoped (`_lib/indexedOnly.ts`), before auth or any read.
  const refused = query && indexedOnly(ctx.env) ? rejectQuery(qRaw, url.searchParams.get('qs'), ctx.env.QUERY_SYNTAX) ?? rejectScope(!!(lens || owner || classes)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })

  // Data is gated (store-specific scope), like /data/*.
  let id: Identity | null = member?.id ?? null
  if (gate && !member) {
    const gated = await st.time('auth', requireViewer(ctx as never))
    if (gated instanceof Response) return gated
    id = gated
  }
  // A path-restricted link (specs/path-scopes.md): a hidden path is absent,
  // and every body (cached or fresh) is filtered on the way out.
  const restriction = restrictionFor(id, ctx.env)
  if (restriction && isHidden(path, restriction)) return hiddenNotFound()
  const out = (res: Response | Promise<Response>) =>
    restriction ? Promise.resolve(res).then(r => filterJson(r, (b: Parameters<typeof restrictSubtree>[0]) => restrictSubtree(b, restriction))) : res

  // A user lens or an owner pool folds the live ledger (assignments repaint
  // attribution, or move bytes in or out of a pool): its cache key carries the head.
  // Everything from here touches D1 or the store, so it all sits under one
  // guard: a D1 stall ("internal error") used to escape from the pre-steps
  // as Cloudflare's raw "Worker threw exception" page (2026-09-28); now it is
  // a 503 the client can retry, with the message the ledger head gave.
  try {
    // `me` → the caller's id, so the cache key (shared across viewers) and the
    // body carry the id, never the literal `me`.
    const resolved = await resolveLens(ctx.env, id, lens)
    if (resolved === null) return new Response(ME_UNRESOLVED, { status: 400 })
    lens = resolved
    const lensTag = lensParam(lens)
    // A `d=<slug>` resolves first (the latest indexed scan it names); an exact
    // `date` is checked alongside the other pre-steps. A miss is a 404, never
    // another scan.
    if (!url.searchParams.has('date')) {
      const scan = await scanArg(ctx.env, url.searchParams)
      if (scan instanceof Response) return scan
      date = scan
    }
    // The static index's hex-run rule: substrings match where they `occur` under it, static or not (`hexRuns.ts`).
    const hx = await hexQuery(ctx.env, query)
    query = hx.query
    const [head, xtra, g, indexed] = await st.time('pre', Promise.all([Promise.resolve(0), hasExtras(ctx.env, date), pathGens(ctx.env, [date]), indexedScan(ctx.env, date, true)]))
    if (!indexed) return noScan('date', date)
    const cacheKey = cacheKeyFor('subtree', cacheEnvTag(ctx.env, ctx.request),
      `${date}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&l=${lensTag}` +
        `&o=${rawOwner ?? ''}&b=${by ?? ''}&D=${depth ?? ''}&cl=${classKey(classes)}&x=${xtra ? 1 : 0}&F=${query && !full ? 0 : 1}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&g=${g}&st=${staticTag(ctx.env, query)}${query ? `&fv=${FILTER_VIEW_V}` : ''}` +
        // The root's answer carries the deployment's `ROOT_LABEL` (kept in the key: dropping it would flush prod's root entries).
        (path === '' ? `&rl=${encodeURIComponent(ctx.env.ROOT_LABEL ?? '')}` : '') +
        // A union root's member read: the union's threshold (absent otherwise, so no other key moves).
        (member?.threshold !== undefined ? `&T=${member.threshold}` : ''),
      storeKey(ctx.env),
    )
    // …and a scan the static index covers for this literal (else `scan-not-indexed`, not a path-store scan).
    if (query && indexedOnly(ctx.env)) {
      const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [date], { firstPaint: !full }))
      if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    // The worker's answer: the view's JSON and what to keep of it — `phase2Ms` (a background full run's) over
    // the viewer-facing default.
    const render = async (o: { phase2Ms?: number; trace?: typeof st.trace } = {}) => {
      // A broken interval-store run is cut out and the view read again (its scans per-scan): never a failed request.
      const view = await ivRetry(() => buildView(ctx.env, { date, path, w, h, minArea, atten, lens, owner, by, maxDepth: depth, query, classes, firstPaint: !!query && !full, trace: o.trace, ...(o.phase2Ms ? { phase2Ms: o.phase2Ms } : {}), ...(member?.threshold !== undefined ? { threshold: member.threshold } : {}) }))
      const body = JSON.stringify({
        date,
        path,
        w,
        h,
        minArea,
        atten,
        tier: view.tier,
        index: view.index,
        ...(lens ? { lens: lensTag } : {}),
        threshold: Math.round(view.threshold),
        nodes: view.nodes,
        truncated: view.truncated,
        ...(owner ? { owner } : {}),
        ...(query ? { q: qRaw, ...hexNote(hx.hexRuns, query.ast), matches: view.matches ?? [], matched: view.matched ?? [], ...(view.matchCount ? { matchCount: view.matchCount } : {}), ...(view.matchesCapped ? { matchesCapped: true } : {}), ...(view.rollup ? { rollup: view.rollup } : {}), ...(view.excluded ? { excluded: view.excluded } : {}), ...(view.firstPaint ? { firstPaint: true } : {}), ...(view.interiors ? { interiors: view.interiors } : {}), partial: view.partial, partialReason: view.partialReason, approximate: view.approximate, approximateReason: view.approximateReason, ...(view.dirsOnly ? { dirsOnly: true } : {}) } : {}),
        ...(view.interiors?.late ? { budgetCut: true } : {}),
        tree: view.tree,
      })
      return { body, keep: keepFor(view.interiors) }
    }
    // A partial answer (phase 2 cut by its time budget) schedules the full one in the background, which
    // replaces it in the colo cache (`upgradePartial`): on the miss that produced it, and on a partial hit.
    const upgrade = (res: Response): Response => {
      res.headers.set('x-cache-upgrade', upgradePartial(ctx.env, cacheKey, () => render({ phase2Ms: UPGRADE_PHASE2_MS }), ctx.waitUntil?.bind(ctx)))
      return res
    }
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return out(isPartial(hit) ? upgrade(hit) : hit)

    const { body, keep } = await render({ trace: st.trace })
    // A phase 2 cut short by its time budget: kept briefly (`keepFor`), so a retry or a second viewer isn't
    // another full recompute; its totals and match counts are exact, only the drawn interiors partial.
    const res = await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keep)
    return out(keep === true ? res : upgrade(res))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    // 409 (not 500): a lens index missing for this scan is deterministic —
    // the client falls back instead of retrying forever.
    if (e instanceof LensUnavailable) return new Response('lens index not available for this scan', { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`subtree failed: ${msg}`, { status: 500 })
  }
}

/** The params a union root refuses: each is one store's axis (the ledger's lens and pools, storage classes), not
 *  yet composed across members. A drill below the root takes them (its member answers). The name filter (`q`) is
 *  composed: `unionFilteredRoot`. */
const UNION_ROOT_UNSCOPED = ['lens', 'o', 'by', 'cl'] as const

/** A union answer's headers: as private as its members' answers; a member's uncacheable one (a budget-cut
 *  partial) makes it so. */
const unionHeaders = (reads: readonly Response[]): Record<string, string> => ({
  'content-type': 'application/json; charset=utf-8',
  'cache-control': reads.map(r => r.headers.get('cache-control')).find(c => c === 'no-store') ?? reads[0]?.headers.get('cache-control') ?? 'no-store',
  'x-cache': reads.map(r => r.headers.get('x-cache') ?? '-').join(','),
})

type FilteredBody = FilteredRoot & { tier: string; index: string; nodes: number; truncated: boolean; budgetCut?: true }

/**
 * A union root's name filter (`q=`): each ready member answers it for its own roots at its resolved scan through
 * its own read path — its own static name index (`STORES_JSON` `vars.STATIC_GEN`) and cache keys; a member without
 * one refuses for its mount (`scan-not-indexed`), never a walk or another store's index. Read twice when more than
 * one member matches: first each under its own budget (its matched total), then those under the union's one
 * threshold from the members' matched totals (`filterThreshold`, `&T=` in their keys). Merged by `unionFiltered`;
 * a refusing member names itself in the `union` vector and `partialReason`. Every member refusing: the first
 * refusal, as its member gave it.
 */
async function unionFilteredRoot(ctx0: SubtreeCtx, view: UnionView, resolved: Resolved, id: Identity | null, o: { w: number; h: number; minArea: number; atten: number; q: string }): Promise<Response> {
  const ready = resolved.members.filter(m => m.date !== null)
  const ask = (m: Member, threshold?: number) =>
    subtree({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) }, true, { id, ...(threshold === undefined ? {} : { threshold }) })
  const reads = await Promise.all(ready.map(m => ask(m)))
  const refused: Refusal[] = []
  const bodies = new Map<number, FilteredBody>()
  for (const [i, r] of reads.entries()) {
    if (r.ok) { bodies.set(i, await r.json() as FilteredBody); continue }
    const x = await refusalOf(ready[i].store, r)
    if (!x) return r
    refused.push(x)
  }
  if (!bodies.size && refused.length) return refusalResponse(refused[0])
  const threshold = filterThreshold([...bodies.values()].reduce((n, b) => n + b.tree.b, 0), o.w, o.h, o.minArea)
  const drawn = [...bodies.keys()].filter(i => bodies.get(i)!.tree.b > 0)
  if (drawn.length > 1) {
    const again = await Promise.all(drawn.map(i => ask(ready[i], threshold)))
    const bad = again.find(r => !r.ok)
    if (bad) return bad
    for (const [k, i] of drawn.entries()) { reads[i] = again[k]; bodies.set(i, await again[k].json() as FilteredBody) }
  }
  const answered = [...bodies.keys()].sort((a, b) => a - b)
  const parts = answered.map(i => ({ store: ready[i].store, date: ready[i].date!, body: bodies.get(i)! }))
  const { tree, ...fields } = unionFiltered(view, ctx0.env.ROOT_LABEL ?? 'all buckets', parts, refused)
  const bs = parts.map(p => p.body)
  return new Response(JSON.stringify({
    date: resolved.date, path: '', w: o.w, h: o.h, minArea: o.minArea, atten: o.atten,
    tier: bs[0]?.tier ?? 'none', index: bs[0]?.index ?? 'none',
    threshold: Math.round(threshold),
    nodes: bs.reduce((n, x) => n + x.nodes, 0),
    truncated: bs.some(x => x.truncated),
    union: filteredVector(resolved, refused),
    q: o.q,
    ...fields,
    ...(bs.some(x => x.budgetCut) ? { budgetCut: true } : {}),
    tree,
  }), { headers: unionHeaders(answered.map(i => reads[i])) })
}

/**
 * `/api/subtree` on a union view (`_lib/union.ts`). The root: every member's root read concurrently at its
 * resolved scan, under one byte threshold from the union's total (the pixel budget is the union's, not each
 * member's), merged — each member's top-level nodes tagged with their store (`src`) and scan (`at`), the
 * members' `(other)`s summed into one, the union's `ROOT_LABEL` at the top, and the resolved vector (`union`)
 * beside it. Below the root: the path's member answers, through its own read path at its resolved scan.
 */
async function unionSubtree(ctx0: SubtreeCtx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  try {
    const got = await resolveUnion(ctx0, view, true)
    if (got instanceof Response) return got
    const { resolved, id } = got
    if (path !== '') {
      const m = await memberOf(resolved, path)
      if (!m) return new Response('path not found', { status: 404 })
      return subtree({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) }, true, { id })
    }
    const scoped = UNION_ROOT_UNSCOPED.filter(k => url.searchParams.get(k))
    if (scoped.length) return new Response(`a union view's root is unscoped (${scoped.join(', ')}: drill into one store)`, { status: 400 })
    const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
    const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
    const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
    const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
    const q = url.searchParams.get('q') ?? ''
    if (q.trim()) return await unionFilteredRoot(ctx0, view, resolved, id, { w, h, minArea, atten, q })
    const ready = resolved.members.filter(m => m.date !== null)
    // Each member's root total (the union's threshold) and its every root (the table lists them all: `unionRoot`).
    const [roots, rows] = await Promise.all([
      Promise.all(ready.map(m => ivRetry(() => readRootAgg(m.ctx.env, { date: m.date!, path: '' })))),
      Promise.all(ready.map(m => ivRetry(() => readRootRows(m.ctx.env, m.date!)))),
    ])
    const total = roots.reduce((n, r) => n + (r?.b ?? 0), 0)
    const threshold = (total * minArea) / (w * h)
    const reads = await Promise.all(ready.map(m => subtree({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) }, true, { id, threshold })))
    const bad = reads.find(r => !r.ok)
    if (bad) return bad
    type Body = { tier: string; index: string; nodes: number; truncated: boolean; budgetCut?: true; tree: ViewNode }
    const bodies = await Promise.all(reads.map(r => r.json() as Promise<Body>))
    const tree = unionRoot(view, ctx0.env.ROOT_LABEL ?? 'all buckets', bodies.map((b, i) => ({ store: ready[i].store, date: ready[i].date!, tree: b.tree, roots: rows[i] })))
    return new Response(JSON.stringify({
      date: resolved.date, path, w, h, minArea, atten,
      tier: bodies[0]?.tier ?? 'none', index: bodies[0]?.index ?? 'none',
      threshold: Math.round(threshold),
      nodes: bodies.reduce((n, x) => n + x.nodes, 0),
      truncated: bodies.some(x => x.truncated),
      union: vectorOf(resolved),
      ...(bodies.some(x => x.budgetCut) ? { budgetCut: true } : {}),
      tree,
    }), { headers: unionHeaders(reads) })
  } catch (e) {
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`subtree failed: ${msg}`, { status: 500 })
  }
}
