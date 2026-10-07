import { type Env, json, requireViewer } from '../../_lib/auth.js'

/** Recorded outcomes for the current staging, not older runs of a re-staged path. */
export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: 'plans store not configured' }, 503)
  const id = Number(new URL(ctx.request.url).searchParams.get('id'))
  if (!Number.isSafeInteger(id) || id <= 0) return json({ error: 'positive plan id required' }, 400)
  const { results } = await ctx.env.DB.prepare(`
    SELECT b.*, r.started_ts, r.finished_ts, r.undo_deadline, r.undo_state
    FROM plan_items i JOIN deletion_bands b ON b.prefix = i.prefix
    JOIN deletion_runs r ON r.run_id = b.run_id
    WHERE i.plan_id = ? AND r.plan_id = i.plan_id AND r.mode = 'real' AND r.started_ts >= i.added_ts
    ORDER BY r.started_ts DESC, b.prefix LIMIT 10000
  `).bind(id).all()
  return json({ bands: results }, 200, { 'cache-control': 'private, max-age=15' })
}
