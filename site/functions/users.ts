// `/users` unfurl: its own title/desc + the deployment's owner-map og:image
// when it ships one (names only; no sizes or $ — see src/UserPage.tsx UsersOgPage).
import { assetImage, unfurlShell } from './_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  ROOT_LABEL?: string
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const origin = new URL(ctx.request.url).origin
  return unfurlShell(ctx, {
    title: `${ctx.env.ROOT_LABEL ?? 'Storage'} — users`,
    desc: 'Who owns what across the scanned buckets: every user’s bytes, largest first.',
    image: await assetImage(ctx, '/og-users.jpg') ?? `${origin}/og.jpg`,
    page: `${origin}/users`,
  })
}
