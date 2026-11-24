/**
 * `GET /api/groups` — the store's bucket groups (`STORES_JSON` `groups`, specs/bucket-groups.md), for the Map's root
 * menu: each group's name (`?g=<name>`), label and member buckets, in config order. `store=<key>` answers for that
 * secondary store. Behind the viewer gate (bucket names are the store's), and a path-restricted link isn't told of a
 * group it would be refused (`groupReachesHidden`). A union view has none (its root has no groups).
 *
 *   → { groups: [{ name: 'pool-a', label: 'pool A', buckets: ['alpha', 'beta'] }] }
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { groupBlock, groupReachesHidden } from '../_lib/groups.js'
import { restrictionFor } from '../_lib/restrict.js'
import { StoreConfigError, storeConfigResponse, storeGroups, withStore } from '../_lib/stores.js'
import { unionFor } from '../_lib/union.js'

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  if (u) return json({ groups: [] })
  let groups
  try {
    groups = Object.values(storeGroups(ctx.env))
  } catch (e) {
    if (e instanceof StoreConfigError) return storeConfigResponse(e)
    throw e
  }
  const restriction = restrictionFor(id, ctx.env)
  return json({ groups: groups.filter(g => !restriction || !groupReachesHidden(g, restriction)).map(groupBlock) })
}
