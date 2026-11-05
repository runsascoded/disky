import { type Env, json, requireViewer } from '../../_lib/auth.js'
import { scanDeletions } from '../../_lib/plans.js'

/** What `/staged`'s sizes at `?scan=` are out of date by (`withoutDeleted`). */
export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: 'plans store not configured' }, 503)
  const scan = new URL(ctx.request.url).searchParams.get('scan')
  if (!scan) return json({ error: 'scan required' }, 400)
  return json({ bands: await scanDeletions(ctx.env.DB, scan) }, 200, { 'cache-control': 'private, max-age=15' })
}
