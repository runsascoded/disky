/** The sweep's deployment settings (`_lib/sweepSettings.ts`): what `/admin` edits and `/staged`'s real-delete confirm states.
 *
 *   GET /api/sweep/settings → {settings: {min_soft_delete_days, updated_by, updated_at} | null, min_soft_delete_days}
 *   PUT /api/sweep/settings {min_soft_delete_days} → the same                                                     admin
 *
 * `settings` is the row (null: none, or a D1 without the table), `min_soft_delete_days` what a dispatch uses now (with no row,
 * the executor's default: 7 on `sweep`, 0 on `plan-sweep`). Each PUT logs its old and new row to `admin_edits`. */
import { type Ctx, json, requireAdmin, requireViewer } from '../../_lib/auth.js'
import { getSweepSettings, minSoftDeleteDays, missingTable, parseSweepSettings, putSweepSettings } from '../../_lib/sweepSettings.js'
import { defaultMinSoftDeleteDays } from '../../../src/softDelete.js'

type C = Ctx & { env: { EXECUTOR?: string } }

export const onRequestGet = async (ctx: C): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ settings: null, min_soft_delete_days: defaultMinSoftDeleteDays(ctx.env.EXECUTOR) }, 200, { 'cache-control': 'no-store' })
  return json({ settings: await getSweepSettings(ctx.env.DB), min_soft_delete_days: await minSoftDeleteDays(ctx.env.DB, ctx.env.EXECUTOR) }, 200, { 'cache-control': 'no-store' })
}

export const onRequestPut = async (ctx: C): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const s = parseSweepSettings(await ctx.request.json().catch(() => null))
  if (typeof s === 'string') return json({ error: s }, 400)
  if (!ctx.env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  try {
    const settings = await putSweepSettings(ctx.env.DB, s, id.email ?? id.name ?? 'admin', Math.floor(Date.now() / 1000))
    return json({ settings, min_soft_delete_days: settings.min_soft_delete_days }, 200, { 'cache-control': 'no-store' })
  } catch (e) {
    if (missingTable(e)) return json({ error: 'not set up on this deployment: apply the sweep-soft-delete migration' }, 503)
    throw e
  }
}
