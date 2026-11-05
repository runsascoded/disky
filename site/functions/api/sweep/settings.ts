/** The sweep's deployment settings (`_lib/sweepSettings.ts`): what `/admin` edits and `/staged`'s real-delete confirm states.
 *
 *   GET /api/sweep/settings → {settings: {min_soft_delete_days, updated_by, updated_at} | null, min_soft_delete_days}
 *   PUT /api/sweep/settings {min_soft_delete_days} → the same                                                     admin
 *
 * `settings` is the row (null: none, or a D1 without the table), `min_soft_delete_days` what a dispatch uses now (7 with no
 * row). Each PUT logs its old and new row to `admin_edits`. Only the `sweep` executor has a soft-delete guard: a deployment on
 * `plan-sweep` (S3 has no soft delete; its undo is its own hold) refuses a PUT that would mean nothing. */
import { type Ctx, json, requireAdmin, requireViewer } from '../../_lib/auth.js'
import { getSweepSettings, minSoftDeleteDays, missingTable, parseSweepSettings, putSweepSettings } from '../../_lib/sweepSettings.js'
import { DEFAULT_MIN_SOFT_DELETE_DAYS } from '../../../src/softDelete.js'

type C = Ctx & { env: { EXECUTOR?: string } }

export const onRequestGet = async (ctx: C): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ settings: null, min_soft_delete_days: DEFAULT_MIN_SOFT_DELETE_DAYS }, 200, { 'cache-control': 'no-store' })
  return json({ settings: await getSweepSettings(ctx.env.DB), min_soft_delete_days: await minSoftDeleteDays(ctx.env.DB) }, 200, { 'cache-control': 'no-store' })
}

export const onRequestPut = async (ctx: C): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const s = parseSweepSettings(await ctx.request.json().catch(() => null))
  if (typeof s === 'string') return json({ error: s }, 400)
  if (ctx.env.EXECUTOR !== 'sweep') return json({ error: 'this deployment\'s executor has no soft-delete guard' }, 409)
  if (!ctx.env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  try {
    const settings = await putSweepSettings(ctx.env.DB, s, id.email ?? id.name ?? 'admin', Math.floor(Date.now() / 1000))
    return json({ settings, min_soft_delete_days: settings.min_soft_delete_days }, 200, { 'cache-control': 'no-store' })
  } catch (e) {
    if (missingTable(e)) return json({ error: 'not set up on this deployment: apply the sweep-soft-delete migration' }, 503)
    throw e
  }
}
