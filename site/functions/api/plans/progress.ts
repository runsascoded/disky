import { type Env, json, requireViewer } from '../../_lib/auth.js'

/** Bounded chronological cumulative samples; rates are derived client-side. */
export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: 'plans store not configured' }, 503)
  const id = new URL(ctx.request.url).searchParams.get('id')
  if (!id) return json({ error: 'id required' }, 400)
  const exists = await ctx.env.DB.prepare('SELECT run_id FROM deletion_runs WHERE run_id = ?').bind(id).first()
  if (!exists) return json({ error: 'no such run' }, 404)
  const { results } = await ctx.env.DB.prepare(`
    SELECT * FROM (
      SELECT bucket, ts, deletes, bytes, gone, overwritten, failed, done, bytes_exact
      FROM deletion_progress WHERE run_id = ? ORDER BY ts DESC, bucket LIMIT 20000
    ) ORDER BY ts, bucket
  `).bind(id).all()
  return json({ samples: results }, 200, { 'cache-control': 'private, max-age=15' })
}
