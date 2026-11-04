import { type Env, json, requireViewer } from '../../_lib/auth.js'
import { runSamples } from '../../_lib/runProgress.js'

/** Bounded chronological cumulative samples; rates are derived client-side. */
export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: 'plans store not configured' }, 503)
  const id = new URL(ctx.request.url).searchParams.get('id')
  if (!id) return json({ error: 'id required' }, 400)
  const exists = await ctx.env.DB.prepare('SELECT run_id FROM deletion_runs WHERE run_id = ?').bind(id).first()
  if (!exists) return json({ error: 'no such run' }, 404)
  return json({ samples: await runSamples(ctx.env.DB, id) }, 200, { 'cache-control': 'private, max-age=15' })
}
