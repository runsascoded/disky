/**
 * POST /api/prefixes  { date, prefixes: [...] }
 *   → { date, stats: { <prefix>: { b, o, d?, a?, us?, cb? } }, groups }
 *
 * Any list of up to `MAX_PREFIXES` prefixes at one scan, with what a table
 * row shows (bytes, objects, created / last-read days, owner shares, class
 * mix), in one batched index read (`_lib/prefixes.ts`) — `/staged`'s items,
 * the action log's prefixes. A prefix gone by that scan is absent from
 * `stats`.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { storeReady } from '../_lib/index.js'
import { MAX_PREFIXES, prefixesAt } from '../_lib/prefixes.js'

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const { env, request } = ctx
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const body = (await request.json().catch(() => null)) as { date?: unknown; prefixes?: unknown } | null
  const date = typeof body?.date === 'string' ? body.date : ''
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return json({ error: 'date=YYYY-MM-DD[THHMM] required' }, 400)
  const prefixes = Array.isArray(body?.prefixes) ? body.prefixes.filter((p): p is string => typeof p === 'string' && /^[a-z0-9]+:\/\/[^/]+\//.test(p)) : []
  if (!prefixes.length || prefixes.length > MAX_PREFIXES) return json({ error: `expected 1–${MAX_PREFIXES} prefixes` }, 400)
  try {
    const { stats, groups } = await prefixesAt(env, date, prefixes)
    return json({ date, stats, groups }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: (e as Error).message }, 503)
  }
}
