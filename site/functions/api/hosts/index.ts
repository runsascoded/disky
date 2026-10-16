/**
 * `GET /api/hosts` — the deployment's capture hosts (D1 `hosts`, specs/hosts-registry.md): each one's
 * store key, label, `~` and root, members only (host labels are private).
 * `POST /api/hosts` — enroll a Mac (an admin's same-origin session; specs/device-api.md): `{id, store, token}`.
 *
 *   → [{id: "m3", store: "primary", label: "m3", home: "Users/ryan"}, {id: "nas", store: "nas", label: "NAS", root: "mnt/user", mark: "nas"}]
 */
import { type Ctx, baseScope, json, requireViewer } from '../../_lib/auth.js'
import { enroll } from '../../_lib/devices.js'
import { hostInfo, loadHosts } from '../../_lib/hosts.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  const member = id.via === 'session' && (id.scopes.includes(baseScope(ctx.env)) || id.scopes.includes('*'))
  if (!member) return json({ error: 'members only' }, 403)
  return json((await loadHosts(ctx.env)).map(hostInfo), 200, { 'cache-control': 'private, no-store' })
}

export const onRequestPost = (ctx: Ctx): Promise<Response> => enroll(ctx)
