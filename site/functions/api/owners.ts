/**
 * GET /api/owners?date=<scan>
 *
 * Per-user owned bytes for a scan — the ownership ledger folded server-side
 * against the floor-free path index (`_lib/ownerTotals.ts`): what `/users`
 * ranks and prices. `{ scan, head, bytes, objects, users: { <id>: { b, mix } } }`
 * (`mix` = class id → bytes, so each user's estate prices like the scan's
 * attribution does); the per-claim rows stay on `/api/estate`.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { ownerTotals } from '../_lib/ownerTotals.js'
import { storeReady } from '../_lib/index.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  // The ownership ledger is the primary store's: `store=<other>` is a 404.
  const notHere = primaryOnly(ctx)
  if (notHere) return notHere
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'ledger backend not configured (DB)' }, 503)
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const date = new URL(request.url).searchParams.get('date') ?? ''
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return json({ error: 'date=YYYY-MM-DD[THHMM] required' }, 400)
  try {
    const t = await ownerTotals(env, date)
    return json({ scan: t.scan, head: t.head, bytes: t.bytes, objects: t.objects, users: t.users, computed: t.computed }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: (e as Error).message }, 503)
  }
}
