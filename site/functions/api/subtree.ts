/** Pixel-budget subtree of any path, served from the index tiers
 * (specs/view-serving.md; the folding lives in `_lib/view.ts`).
 *
 *   GET /api/subtree?date=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>]
 *                    [&lens=user:<id>][&o=owned|unowned][&q=<name filter>]
 *
 * The page's scope axes (specs/view-serving.md §2) are applied server-side:
 * the owner axis (`lens` for a user, `o` for the pools), the name filter
 * (`q`). Responses are immutable per (date, path, w₁₂₈, h₁₂₈, minArea,
 * atten, scope) plus the ledger head under a user lens — and cached in the
 * edge cache accordingly. w/h arrive quantized-up to 128px so resizes mostly
 * re-hit the cache.
 */
import { type Env, requireViewer } from '../_lib/auth.js'
import { pathGens, storeReady, type Lens } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { parseOwner, queryParam, QueryError, classKey, parseClasses } from '../_lib/scope.js'
import { hasExtras } from '../_lib/extras.js'
import { ATTEN_DEFAULT, buildView, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT } from '../_lib/view.js'
import { cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'


type SubtreeCtx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = (ctx0: SubtreeCtx): Promise<Response> => subtree(ctx0, true)

/** Fill the edge cache for a subtree URL without a caller identity: what a
 * share card's unfurl does so the clickthrough's first paint is warm
 * (`og/serve.ts`). The response never leaves the Worker. */
export const warmSubtree = (env: Env, url: string, waitUntil?: (p: Promise<unknown>) => void): Promise<Response> =>
  subtree({ request: new Request(url), env, waitUntil }, false)

async function subtree(ctx0: SubtreeCtx, gate: boolean): Promise<Response> {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const st = serverTiming()
  if (!storeReady(ctx.env)) {
    return new Response('subtree API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

  // Optional lens: `lens=user:<id>` — a treemap of that user's bytes, read
  // from the by-user sort.
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
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  const query = qp.query

  // Data is gated (store-specific scope), like /data/*.
  if (gate) {
    const gated = await st.time('auth', requireViewer(ctx as never))
    if (gated instanceof Response) return gated
  }

  // A user lens or an owner pool folds the live ledger (assignments repaint
  // attribution, or move bytes in or out of a pool): its cache key carries the head.
  // Everything from here touches D1 or the store, so it all sits under one
  // guard: a D1 stall ("internal error") used to escape from the pre-steps
  // as Cloudflare's raw "Worker threw exception" page (2026-09-28); now it is
  // a 503 the client can retry, with the message the ledger head gave.
  try {
    const [head, xtra, g] = await st.time('pre', Promise.all([lens && ctx.env.DB || owner && await hasLedger(ctx.env) ? ledgerHead(ctx.env) : Promise.resolve(0), hasExtras(ctx.env, date), pathGens(ctx.env, [date])]))
    const cacheKey = cacheKeyFor('subtree',
      `${date}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&l=${lensRaw ?? ''}` +
        `&o=${rawOwner ?? ''}&b=${by ?? ''}&D=${depth ?? ''}&cl=${classKey(classes)}&x=${xtra ? 1 : 0}&F=${query && !full ? 0 : 1}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&g=${g}`,
      storeKey(ctx.env),
    )
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return hit

    const view = await buildView(ctx.env, { date, path, w, h, minArea, atten, lens, owner, by, maxDepth: depth, query, classes, firstPaint: !!query && !full, trace: st.trace })
    const body = JSON.stringify({
      date,
      path,
      w,
      h,
      minArea,
      atten,
      tier: view.tier,
      index: view.index,
      ...(lensRaw ? { lens: lensRaw } : {}),
      threshold: Math.round(view.threshold),
      nodes: view.nodes,
      truncated: view.truncated,
      ...(owner ? { owner } : {}),
      ...(query ? { q: qRaw, matches: view.matches, matched: view.matched ?? [], ...(view.excluded ? { excluded: view.excluded } : {}), ...(view.firstPaint ? { firstPaint: true } : {}), partial: view.partial, partialReason: view.partialReason, approximate: view.approximate, approximateReason: view.approximateReason } : {}),
      tree: view.tree,
    })
    return await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found', { status: 404 })
    // 409 (not 500): a lens index missing for this scan is deterministic —
    // the client falls back instead of retrying forever.
    if (e instanceof LensUnavailable) return new Response('lens index not available for this scan', { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`subtree failed: ${msg}`, { status: 500 })
  }
}
