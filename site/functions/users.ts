// `/users` unfurl: its own title/desc + the deployment's owner-map og:image
// when it ships one (names only; no sizes or $ — see src/UserPage.tsx UsersOgPage).
import { assetImage, unfurlShell } from './_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const origin = new URL(ctx.request.url).origin
  return unfurlShell(ctx, {
    title: 'Marin GCS usage — users',
    desc: 'Who owns what across the marin-* buckets: every user’s bytes, largest first.',
    image: await assetImage(ctx, '/og-users.jpg') ?? `${origin}/og.jpg`,
    page: `${origin}/users`,
  })
}
