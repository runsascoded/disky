/** GET /api/me — who the caller is, as the owner-keyed APIs see them:
 *
 *   → { email, user, scopes, admin, via }
 *
 * `user` is the caller's canonical owner id (`_lib/me.ts`): what `user=me`
 * (`/api/estate`), `lens=user:me` (`/api/subtree`, `/api/diff`,
 * `/api/series`) and `owner: '@me'` (`POST /api/actions`) resolve to, and how
 * `/api/owners` keys them. Null when the identity has no email (a guest link
 * without one). Any viewer; signed out = 401. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { ownerIdFor } from '../_lib/me.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  const user = id.email ? await ownerIdFor(ctx.env, id.email) : null
  return json({ email: id.email, user, scopes: id.scopes, admin: id.admin, via: id.via }, 200, { 'cache-control': 'private, no-store' })
}
