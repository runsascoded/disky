/** What changed under a path between two scans, read server-side from both
 * scans' index tiers at one shared byte floor (`_lib/view.ts` `buildDiff`).
 *
 *   GET /api/diff?from=<scan>&to=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>][&top=<n>]
 *                 [&lens=user:<id>|user:me][&o=owned|unowned][&q=<name filter>][&summary=1][&depth=<levels>]
 *
 * `summary=1` answers with the totals only (both sides' scoped root reads,
 * no walk — `rows` empty): the section's headline, seconds before the rows.
 *
 * Same scope axes as `/api/subtree`; the response is the treemap's row list
 * (`{ rows, total_a, total_b, objects_a, objects_b, expansions, truncated }`),
 * immutable per (from, to, path, budget, scope) plus the ledger head under a
 * user lens, and edge-cached accordingly.
 */
import { type Env, requireViewer } from '../_lib/auth.js'
import { ivRetry, pathGens, withPathStore, storeReady, type Lens } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { classKey, parseClasses, parseOwner, queryParam, QueryError } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildDiff, FILTER_VIEW_V, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'
import { hexNote, hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, isPartial, keepFor, serverTiming, UPGRADE_PHASE2_MS, upgradePartial } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId } from '../../src/scanSlug.js'
import { scanArg } from '../_lib/scanArg.js'
import { traceJoins } from '../_lib/shared.js'

export const onRequestGet = async (ctx0: { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = withPathStore(ctx1)
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) {
    return new Response('diff API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const from = url.searchParams.get('from') ?? ''
  const to = url.searchParams.get('to') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  const top = Math.min(5000, Number(url.searchParams.get('top')) || 500)
  const summary = url.searchParams.get('summary') === '1'
  const depth = Number(url.searchParams.get('depth')) || undefined
  if (!isScanId(from) || !isScanId(to)) return new Response('bad from/to', { status: 400 })
  if (from >= to) return new Response('from must precede to', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

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
  const classes = parseClasses(url.searchParams.get('cl'))
  const qRaw = url.searchParams.get('q') ?? ''
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

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  // Both ends must be indexed scans: a miss is a 404, never another scan.
  const miss = (await Promise.all(['from', 'to'].map(key => scanArg(ctx.env, url.searchParams, key)))).find(r => r instanceof Response)
  if (miss) return miss

  // One guard over the D1 pre-step, the cache match and the build (see
  // subtree.ts): a D1 stall becomes a retryable 503, not a raw 500 page.
  try {
    // `user:me` → the caller's id: the shared cache key and the body carry the id.
    const resolved = await resolveLens(ctx.env, gated, lens)
    if (resolved === null) return new Response(ME_UNRESOLVED, { status: 400 })
    lens = resolved
    const lensTag = lensParam(lens)
    // The static index's hex-run rule: substrings match where they `occur` under it, static or not (`hexRuns.ts`).
    const hx = await hexQuery(ctx.env, query)
    query = hx.query
    const [head, g] = await st.time('pre', Promise.all([lens && ctx.env.DB || owner && await hasLedger(ctx.env) ? ledgerHead(ctx.env) : Promise.resolve(0), pathGens(ctx.env, [from, to])]))
    const cacheKey = cacheKeyFor('diff', cacheEnvTag(ctx.env, ctx.request),
      `${from}/${to}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&n=${top}&l=${lensTag}` +
        `&o=${rawOwner ?? ''}&cl=${classKey(classes)}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&s=${summary ? 1 : 0}&D=${depth ?? ''}&g=${g}&st=${staticTag(ctx.env, query)}${query ? `&fv=${FILTER_VIEW_V}` : ''}`,
      storeKey(ctx.env),
    )
    // …and both scans covered by the static index for this literal (else `scan-not-indexed`).
    if (query && indexedOnly(ctx.env)) {
      const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [from, to]))
      if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    // The worker's answer: the diff's JSON and what to keep of it — `phase2Ms` (a background full run's) over
    // the viewer-facing default.
    const render = async (o: { phase2Ms?: number; trace?: typeof st.trace } = {}) => {
      const diff = await ivRetry(() => buildDiff(ctx.env, { from, to, path, w, h, minArea, atten, top, lens, owner, query, classes, summary, depth, trace: o.trace, ...(o.phase2Ms ? { phase2Ms: o.phase2Ms } : {}) }))
      const body = JSON.stringify({
        prev: from,
        curr: to,
        path,
        ...(lens ? { lens: lensTag } : {}),
        ...(owner ? { owner } : {}),
        ...(query ? { q: qRaw, ...hexNote(hx.hexRuns, query.ast) } : {}),
        ...diff,
        threshold: Math.round(diff.threshold),
        ...(diff.interiors?.late ? { budgetCut: true } : {}),
      })
      return { body, keep: keepFor(diff.interiors) }
    }
    // A partial answer schedules the full one in the background (`upgradePartial`, as subtree.ts).
    const upgrade = (res: Response): Response => {
      res.headers.set('x-cache-upgrade', upgradePartial(ctx.env, cacheKey, () => render({ phase2Ms: UPGRADE_PHASE2_MS }), ctx.waitUntil?.bind(ctx)))
      return res
    }
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return isPartial(hit) ? upgrade(hit) : hit

    const { body, keep } = await render({ trace: st.trace })
    // A phase 2 cut short by its time budget: kept briefly (`keepFor`, as subtree.ts).
    const res = await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keep)
    return keep === true ? res : upgrade(res)
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found in either scan', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof LensUnavailable) return new Response('lens index not available for a scan', { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}
