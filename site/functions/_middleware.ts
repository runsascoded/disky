// Every HTML page response gets its card stamped into `<head>`
// (specs/done/dogi.md): crawlers never run the React router, so the edge says what
// each URL shows. Everything else passes through untouched, except an
// `/assets/` miss (`assetMiss`).
import { restrictGate } from './_lib/restrict.js'
import { coloPutsSettled } from './_lib/index.js'
import { anonymousPage, stampPage, type OgEnv } from './_lib/og/serve.js'

const isHtml = (res: Response): boolean => (res.headers.get('content-type') ?? '').startsWith('text/html')

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

export const onRequest = async (ctx: { request: Request; env: OgEnv; next: () => Promise<Response>; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const blocked = await restrictGate(ctx.request, ctx.env as never)
  if (blocked) return blocked
  const url = new URL(ctx.request.url)
  if (ctx.request.method === 'GET') {
    const anonymous = await anonymousPage(ctx.env, url)
    if (anonymous) return anonymous
  }
  const res = await ctx.next()
  // The reader puts decoded row groups in the colo without waiting (`putColoGroup`); a Worker drops work still in
  // flight once the response is sent, so keep the isolate alive until those puts land.
  ctx.waitUntil?.(coloPutsSettled())
  if (url.pathname.startsWith('/assets/')) return isHtml(res) ? assetMiss() : res
  if (ctx.request.method !== 'GET' || !isHtml(res)) return res
  try {
    return await stampPage(ctx.env, url, res)
  } catch (e) {
    // An unfurl detail must never break the page.
    console.log(`og stamp failed: ${(e as Error).message}`)
    return res
  }
}
