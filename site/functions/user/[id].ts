// `/user/:id` unfurl: the user's display name in the title (name + handle is
// the public ceiling — never sizes or $).
import { defaultName } from '../../src/identityRegistry.js'
import { assetImage, unfurlShell } from '../_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  ROOT_LABEL?: string
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const id = decodeURIComponent(url.pathname.split('/')[2] ?? '')
  // Unauthenticated: the unfurl never reads the roster — the id's first segment.
  const name = id ? defaultName(id) : 'User'
  // Per-user card when `scripts/shoot-user-ogs.mjs` has generated one;
  // otherwise the shared owner-map card, otherwise the site's.
  const image = await assetImage(ctx, `/og-user/${encodeURIComponent(id)}.jpg`)
    ?? await assetImage(ctx, '/og-users.jpg')
    ?? `${url.origin}/og.jpg`
  return unfurlShell(ctx, {
    title: `${name} — ${ctx.env.ROOT_LABEL ?? 'Storage'}`,
    desc: 'Per-user storage breakdown: what they own, and what was assigned to them.',
    image,
    page: `${url.origin}${url.pathname}`,
  })
}
