/** One person's estate for a scan — what `/user/:id` shows, folded
 * server-side from the index tiers and the live ownership ledger (no
 * `tree.json`):
 *
 *   GET /api/estate?date=<scan>&user=<canonical id>
 *   → { user, date, head, bytes, objects, mix, claims }
 *
 * - `bytes` / `mix`: their owned bytes (+ storage-class mix), claims
 *   applied — the same numbers `/users` shows.
 * - `objects`: the objects under their live claims (the index has no
 *   per-user object count outside a claim).
 * - `claims`: their live owner claims, sized from the index.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { canonId, loadRegistry } from '../_lib/identity.js'
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
  const url = new URL(request.url)
  const date = url.searchParams.get('date') ?? ''
  const user = url.searchParams.get('user') ?? ''
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'date=YYYY-MM-DD required' }, 400)
  if (!/^[a-z0-9_-]+$/.test(user)) return json({ error: 'user=<canonical id> required' }, 400)

  try {
    const [totals, reg] = await Promise.all([ownerTotals(env, date), loadRegistry(env)])
    const mine = (who: string | null | undefined) => !!who && canonId(who, reg) === user
    // The body keys users by the index's usr (a canonical id) or a claimant
    // (an email) — canonicalize both.
    const owned = Object.entries(totals.users).find(([k]) => mine(k))?.[1] ?? null
    const claims = totals.claims
      .filter(c => mine(c.owner))
      .map(c => ({ prefix: c.prefix, ts: c.ts, bytes: c.bytes, objects: c.objects, ...(c.repainted_by ? { repainted_by: c.repainted_by } : {}) }))
    const objects = claims.filter(c => !c.repainted_by).reduce((s, c) => s + c.objects, 0)
    return json({ user, date, head: totals.head, bytes: owned?.b ?? 0, objects, mix: owned?.mix ?? {}, claims }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: (e as Error).message }, 503)
  }
}
