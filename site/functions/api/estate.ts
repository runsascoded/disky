/** One person's estate for a scan — what `/user/:id` shows, folded
 * server-side from the index tiers and the live ownership ledger (no
 * `tree.json`):
 *
 *   GET /api/estate?date=<scan>&user=<canonical id | me>
 *   → { user, date, head, bytes, objects, mix, assignments }
 *
 * `user=me` is the caller's canonical id (`_lib/me.ts`); the body's `user`
 * is always the resolved id.
 *
 * - `bytes` / `mix`: their owned bytes (+ storage-class mix), assignments
 *   applied — the same numbers `/users` shows.
 * - `objects`: the objects under their live assignments (the index has no
 *   per-user object count outside an assignment).
 * - `assignments`: their live owner assignments, sized from the index.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { hasLedger } from '../_lib/ledger.js'
import { canonId, loadRegistry } from '../_lib/identity.js'
import { ownerTotals } from '../_lib/ownerTotals.js'
import { storeReady } from '../_lib/index.js'
import { ME_UNRESOLVED, resolveUser } from '../_lib/me.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  // The ownership ledger is the primary store's: `store=<other>` is a 404.
  const notHere = primaryOnly(ctx)
  if (notHere) return notHere
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'ledger backend not configured (DB)' }, 503)
  // cw's D1 (and any lineage without the ledger tables) has no ownership to read.
  if (!(await hasLedger(env))) return json({ error: 'no ownership ledger on this deployment' }, 404)
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const url = new URL(request.url)
  const date = url.searchParams.get('date') ?? ''
  const userRaw = url.searchParams.get('user') ?? ''
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'date=YYYY-MM-DD required' }, 400)
  if (!/^[a-z0-9_-]+$/.test(userRaw)) return json({ error: 'user=<canonical id> or user=me required' }, 400)

  try {
    const user = await resolveUser(env, gated, userRaw)
    if (user == null) return json({ error: ME_UNRESOLVED }, 400)
    const [totals, reg] = await Promise.all([ownerTotals(env, date), loadRegistry(env)])
    const mine = (who: string | null | undefined) => !!who && canonId(who, reg) === user
    // The body keys users by the index's usr (a canonical id) or an assignee
    // (an email) — canonicalize both.
    const owned = Object.entries(totals.users).find(([k]) => mine(k))?.[1] ?? null
    const assignments = totals.assignments
      .filter(c => mine(c.owner))
      .map(c => ({ prefix: c.prefix, ts: c.ts, bytes: c.bytes, objects: c.objects, ...(c.repainted_by ? { repainted_by: c.repainted_by } : {}) }))
    const objects = assignments.filter(c => !c.repainted_by).reduce((s, c) => s + c.objects, 0)
    return json({ user, date, head: totals.head, bytes: owned?.b ?? 0, objects, mix: owned?.mix ?? {}, assignments }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: (e as Error).message }, 503)
  }
}
