/** Two jobs on every request:
 *   1. `/api/*` passes the gate (spec `specs/done/pages-auth.md`); the SPA shell
 *      and assets stay public so the wall has somewhere to render.
 *   2. A scan-route document (`/r2/ctbk`, `/file/…`) gets its Open Graph tags
 *      rewritten to a per-path treemap card — crawlers don't run JS, so the
 *      static `index.html` card is the only one they'd otherwise ever see. */
import { gateApi, gateFor } from '../cfn/auth'
import { type Env, isOpen } from '../cfn/env'
import { ogRoute } from '../cfn/og'

export const onRequest: PagesFunction<Env> = async ({ request, env, next }) => {
  const url = new URL(request.url)
  if (url.pathname.startsWith('/api/')) {
    // Open demo: no gate, every `/api/*` route served without a session.
    if (isOpen(env)) return next()
    const { gate, audit } = gateFor(env, request)
    return gateApi(gate, request, next, audit)
  }

  const res = await next()
  const route = ogRoute(decodeURIComponent(url.pathname))
  if (!route) return res
  if (!(res.headers.get('content-type') ?? '').includes('text/html')) return res

  const image = `${url.origin}/og/${route.key}`
  const pageUrl = `${url.origin}${url.pathname}`
  const content: Record<string, string> = {
    'og:title': route.title,
    'og:image': image,
    'og:image:width': '1200',
    'og:image:height': '630',
    'og:url': pageUrl,
    'twitter:title': route.title,
    'twitter:image': image,
  }
  return new HTMLRewriter()
    .on('meta', {
      element(el) {
        const key = el.getAttribute('property') ?? el.getAttribute('name')
        if (key && key in content) el.setAttribute('content', content[key])
      },
    })
    .on('title', { element(el) { el.setInnerContent(route.title) } })
    .transform(res)
}
