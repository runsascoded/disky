// Every HTML page response gets its card stamped into `<head>`
// (specs/done/dogi.md): crawlers never run the React router, so the edge says what
// each URL shows. Everything else passes through untouched, except an
// `/assets/` miss (`assetMiss`). Every response, whatever answered it (a Function,
// a cached body, the gate, a static asset), leaves with the build that served it
// (`x-disky-build: <sha> <time>`, `_lib/build.ts`), and is counted: one usage point
// per request (route, caller, cache tier, the R2/D1/KV it cost; `_lib/usage.ts`,
// specs/usage-metrics.md), written after the response.
import { withBuild } from './_lib/build.js'
import { hostRedirect } from './_lib/hostRedirect.js'
import { restrictGate } from './_lib/restrict.js'
import { footerCacheDrain } from './_lib/footerCache.js'
import { coloPutsSettled, ivRefusal } from './_lib/index.js'
import { anonymousPage, stampPage, type OgEnv } from './_lib/og/serve.js'
import { clearedOnSignIn, silentAttempt } from './_lib/silentSignIn.js'
import { captureWrite, readBody } from './_lib/activityHook.js'
import { sizeAssignments } from './_lib/activitySize.js'
import { serverKind } from './_lib/activityShape.js'
import type { Ctx } from './_lib/auth.js'
import { instrumentBindings, Meter, metered, usagePoint, type UsageEnv, writeUsage } from './_lib/usage.js'

const isHtml = (res: Response): boolean => (res.headers.get('content-type') ?? '').startsWith('text/html')

// This isolate's requests so far: Server-Timing `isolate` says `cold` on its first (a TTFB floor outside every clock
// here, isolate start-up and bundle evaluation included, shows up as a difference between cold and warm).
let served = 0

/** `res` with the middleware's own phases first in its Server-Timing (a copy when its headers are immutable). */
function withMiddlewareTiming(res: Response, phases: [string, number][], nth: number): Response {
  const own = [...phases.map(([k, ms]) => `${k};dur=${Math.round(ms)}`), `isolate;dur=0;desc="${nth === 1 ? 'cold' : `warm ${nth}`}"`].join(', ')
  const st = res.headers.get('server-timing')
  const v = st ? `${own}, ${st}` : own
  try {
    res.headers.set('server-timing', v)
    return res
  } catch {
    const copy = new Response(res.body, res)
    copy.headers.set('server-timing', v)
    return copy
  }
}

// A hashed bundle URL that names no file in this deployment. Static Pages
// answers it through the SPA fallback (`_redirects`' `/* /index.html 200`; a
// `404` rule there is silently ignored, and any redirect rule shadows the real
// assets too), i.e. HTML for a `.js` URL, which the zone then lets browsers
// cache for hours: right after a deploy, an edge still on the previous
// deployment answers the NEW entry bundle that way and the page stays blank
// until a hard refresh (gcs, 2026-10-09). A no-store 404 instead: the load
// fails loudly (`index.html`'s boot watchdog retries it) and nothing caches
// it, so the retry reaches the new deployment. `_routes.json` routes
// `/assets/*` here for this; a hit passes through as Pages served it.
export const assetMiss = (): Response => new Response('asset not found\n', {
  status: 404,
  headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
})

type MiddlewareCtx = { request: Request; env: OgEnv & UsageEnv; next: () => Promise<Response>; waitUntil?: (p: Promise<unknown>) => void; data?: Record<string, unknown> }

export const onRequest = async (ctx: MiddlewareCtx): Promise<Response> => {
  const m = new Meter()
  // What `serve` leaves running past the response (the colo puts, the footer LRU's writes): the point waits for it,
  // so its reads are counted.
  const left: Promise<unknown>[] = []
  const wu = ctx.waitUntil
  const sctx: MiddlewareCtx = wu ? { ...ctx, waitUntil: p => { left.push(p); wu(p) } } : ctx
  const res = withBuild(await metered(m, ctx, () => { instrumentBindings(ctx.env); return serve(sctx) }))
  if (wu && ctx.env.USAGE) {
    const wallMs = Date.now() - m.t0
    wu(Promise.allSettled(left)
      .then(() => writeUsage(ctx.env, usagePoint(m, ctx.request, res, wallMs, ctx.env.PROD_HOST)))
      .catch(e => console.log(`usage point failed: ${(e as Error).message}`)))
  }
  return res
}

async function serve(ctx: MiddlewareCtx): Promise<Response> {
  const nth = ++served
  const t0 = Date.now()
  const phases: [string, number][] = []
  const mark = (k: string, from: number) => { phases.push([k, Date.now() - from]); return Date.now() }
  // a renamed deployment's old hosts: before the gate, so a signed-out visitor follows a posted link to the new host
  const moved = hostRedirect(ctx.request, ctx.env as never)
  if (moved) return moved
  const blocked = await restrictGate(ctx.request, ctx.env as never)
  let t = mark('mw-gate', t0)
  if (blocked) return withMiddlewareTiming(blocked, [...phases, ['mw', Date.now() - t0]], nth)
  // No session, on a deployment with OA's broker: one silent try before the wall (specs/oa-login-rp.md phase 2).
  const silent = await silentAttempt(ctx)
  if (silent) return silent
  const url = new URL(ctx.request.url)
  if (ctx.request.method === 'GET') {
    const anonymous = await anonymousPage(ctx.env, url)
    t = mark('mw-anon', t)
    if (anonymous) return withMiddlewareTiming(anonymous, [...phases, ['mw', Date.now() - t0]], nth)
  }
  // A write that may be an activity event (specs/done/activity-feed.md): its body is read from a clone before the route
  // consumes it, and it is recorded once the route answers 2xx.
  const activityKind = serverKind(ctx.request.method, url.pathname)
  const body = activityKind ? readBody(ctx.request) : null
  let res0: Response
  try {
    res0 = await ctx.next()
  } catch (e) {
    // An interval-store refusal a route let escape (`withPathStore`'s pointer read runs before most routes' own
    // handling): its 503 + `Retry-After`, not the runtime's bare 500. Anything else stays thrown.
    const refusal = ivRefusal(e)
    if (!refusal) throw e
    res0 = refusal
  }
  res0 = clearedOnSignIn(ctx.request, ctx.env, res0)
  mark('mw-next', t)
  // Server-Timing on API answers only (pages and assets keep their headers as Pages sent them).
  const res = url.pathname.startsWith('/api/') ? withMiddlewareTiming(res0, [...phases, ['mw', Date.now() - t0]], nth) : res0
  if (activityKind && res.ok && ctx.waitUntil) {
    const wait = ctx.waitUntil.bind(ctx)
    wait(captureWrite(ctx as unknown as Ctx, activityKind, body, wait, Date.now(), sizeAssignments))
  }
  // The reader puts decoded row groups in the colo without waiting (`putColoGroup`); a Worker drops work still in
  // flight once the response is sent, so keep the isolate alive until those puts land. The footer LRU's
  // queued writes (fills, access stamps: `_lib/footerCache.ts`) start here, after the response.
  ctx.waitUntil?.(Promise.all([coloPutsSettled(), footerCacheDrain()]).then(() => undefined))
  if (url.pathname.startsWith('/assets/')) return isHtml(res) ? assetMiss() : res
  if (ctx.request.method !== 'GET' || !isHtml(res)) return res
  try {
    return await stampPage(ctx.env, url, res, ctx.waitUntil ? p => ctx.waitUntil!(p) : undefined)
  } catch (e) {
    // An unfurl detail must never break the page.
    console.log(`og stamp failed: ${(e as Error).message}`)
    return res
  }
}
