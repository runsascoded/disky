/** Predictable latest-scan map image shortcut; keep the canonical image
 * endpoint's authentication, privacy policy and cache handling unchanged. */
export const onRequestGet = (ctx: { request: Request }): Response => {
  const url = new URL(ctx.request.url)
  url.pathname = '/og/map.png'
  return new Response(null, {
    status: 302,
    headers: { location: url.href, 'cache-control': 'no-store' },
  })
}
