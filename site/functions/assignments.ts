// `/assignments` unfurl: the page's own title and description (no names or
// sizes — a crawler is unauthenticated), and the site's image.
import { assetImage, unfurlShell } from './_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const origin = new URL(ctx.request.url).origin
  return unfurlShell(ctx, {
    title: 'Assignments: who assigned what to whom',
    desc: 'Ownership assignments by assigner × assignee, in bytes; off the diagonal, someone assigned another person’s data.',
    image: await assetImage(ctx, '/og-assignments.jpg') ?? `${origin}/og.jpg`,
    page: `${origin}/assignments`,
  })
}
