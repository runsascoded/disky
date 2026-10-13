/** Pixel-budget subtree of any path, served from the index tiers
 * (specs/view-serving.md; the folding lives in `_lib/view.ts`).
 *
 *   GET /api/subtree?date=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>]
 *                    [&lens=user:<id>|user:me][&o=owned|unowned][&q=<name filter>]
 *
 * The page's scope axes (specs/view-serving.md §2) are applied server-side:
 * the owner axis (`lens` for a user, `o` for the pools), the name filter
 * (`q`). Responses are immutable per (date, path, w₁₂₈, h₁₂₈, minArea,
 * atten, scope) plus the ledger head under a user lens — and cached in the
 * edge cache accordingly. w/h arrive quantized-up to 128px so resizes mostly
 * re-hit the cache.
 */
import { type Env, type Identity, requireViewer } from '../_lib/auth.js'
import { ivRetry, pathGens, withPathStore, storeReady, type Lens } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { parseOwner, queryParam, QueryError, classKey, parseClasses } from '../_lib/scope.js'
import { hasExtras } from '../_lib/extras.js'
import { ATTEN_DEFAULT, buildView, FILTER_VIEW_V, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'
import { hexNote, hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, isPartial, keepFor, serverTiming, UPGRADE_PHASE2_MS, upgradePartial } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId } from '../../src/scanSlug.js'
import { indexedScan, noScan, scanArg } from '../_lib/scanArg.js'
import { traceJoins } from '../_lib/shared.js'


type SubtreeCtx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = (ctx0: SubtreeCtx): Promise<Response> => subtree(ctx0, true)

/** Fill the edge cache for a subtree URL without a caller identity: what a
 * share card's unfurl does so the clickthrough's first paint is warm
 * (`og/serve.ts`). The response never leaves the Worker. */
export const warmSubtree = (env: Env, url: string, waitUntil?: (p: Promise<unknown>) => void): Promise<Response> =>
  subtree({ request: new Request(url), env, waitUntil }, false)

async function subtree(ctx0: SubtreeCtx, gate: boolean): Promise<Response> {
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
  let id: Identity | null = null
  if (gate) {
    const gated = await st.time('auth', requireViewer(ctx as never))
    if (gated instanceof Response) return gated
    id = gated
  }

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
    const [head, xtra, g, indexed] = await st.time('pre', Promise.all([lens && ctx.env.DB || owner && await hasLedger(ctx.env) ? ledgerHead(ctx.env) : Promise.resolve(0), hasExtras(ctx.env, date), pathGens(ctx.env, [date]), indexedScan(ctx.env, date, true)]))
    if (!indexed) return noScan('date', date)
    const cacheKey = cacheKeyFor('subtree', cacheEnvTag(ctx.env, ctx.request),
      `${date}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&l=${lensTag}` +
        `&o=${rawOwner ?? ''}&b=${by ?? ''}&D=${depth ?? ''}&cl=${classKey(classes)}&x=${xtra ? 1 : 0}&F=${query && !full ? 0 : 1}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&g=${g}&st=${staticTag(ctx.env, query)}${query ? `&fv=${FILTER_VIEW_V}` : ''}` +
        // The root's answer carries the deployment's `ROOT_LABEL` (kept in the key: dropping it would flush prod's root entries).
        (path === '' ? `&rl=${encodeURIComponent(ctx.env.ROOT_LABEL ?? '')}` : ''),
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
      const view = await ivRetry(() => buildView(ctx.env, { date, path, w, h, minArea, atten, lens, owner, by, maxDepth: depth, query, classes, firstPaint: !!query && !full, trace: o.trace, ...(o.phase2Ms ? { phase2Ms: o.phase2Ms } : {}) }))
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
    if (hit) return isPartial(hit) ? upgrade(hit) : hit

    const { body, keep } = await render({ trace: st.trace })
    // A phase 2 cut short by its time budget: kept briefly (`keepFor`), so a retry or a second viewer isn't
    // another full recompute; its totals and match counts are exact, only the drawn interiors partial.
    const res = await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keep)
    return keep === true ? res : upgrade(res)
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
