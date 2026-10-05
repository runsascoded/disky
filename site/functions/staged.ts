// `/staged` unfurl: what the open plan holds, by count only (a crawler is
// unauthenticated — no prefixes, sizes or names), and the site's image.
import { assetImage, unfurlShell } from './_lib/unfurl.js'
import { openPlanId } from './_lib/plans.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  DB?: D1Database
}

/** The unfurl's description for an open plan of `n` prefixes (or none). */
export const stagedDesc = (plan: number | null, n: number): string =>
  plan == null || n === 0
    ? 'Nothing is staged for deletion. Stage prefixes from the map; an admin reviews and dispatches here.'
    : `${n.toLocaleString('en-US')} ${n === 1 ? 'prefix' : 'prefixes'} staged for deletion · plan #${plan} · nothing is deleted until an admin dispatches it.`

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const origin = new URL(ctx.request.url).origin
  let plan: number | null = null
  let n = 0
  if (ctx.env.DB) {
    try {
      plan = await openPlanId(ctx.env.DB)
      if (plan != null) n = (await ctx.env.DB.prepare('SELECT COUNT(*) AS n FROM plan_items WHERE plan_id = ?').bind(plan).first<{ n: number }>())?.n ?? 0
    } catch {
      // A deployment without the staging tables unfurls as "nothing staged".
    }
  }
  return unfurlShell(ctx, {
    title: n ? `Staged for deletion (${n.toLocaleString('en-US')})` : 'Staged for deletion',
    desc: stagedDesc(plan, n),
    image: await assetImage(ctx, '/og-staged.jpg') ?? `${origin}/og.jpg`,
    page: `${origin}/staged`,
  })
}
