/**
 * `GET /api/filter-caps` — what the map's filter accepts on this deployment, for the filter box (its inline
 * refusal and help, `_lib/indexedOnly.ts`), and the store root's label (`ROOT_LABEL`, the unfiltered view's
 * root crumb), for a page whose view was refused and so has no tree to name its root. No data, no gate:
 * deployment config only. `store=<key>` answers for that secondary store (its own `ROOT_LABEL`).
 *
 *   → { indexedOnly: true, rootLabel: 'Marin CoreWeave', rootTitle: '…' }   (`FILTER_INDEXED_ONLY=1`: one literal
 *     substring of a name, unscoped; `rootLabel` / `rootTitle` (`ROOT_TITLE`, the root crumb's tooltip) absent when
 *     the deployment sets none)
 */
import { type Ctx, json } from '../_lib/auth.js'
import { indexedOnly } from '../_lib/indexedOnly.js'
import { withStore } from '../_lib/stores.js'

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  return json({ indexedOnly: indexedOnly(ctx.env), ...(ctx.env.ROOT_LABEL ? { rootLabel: ctx.env.ROOT_LABEL } : {}), ...(ctx.env.ROOT_TITLE ? { rootTitle: ctx.env.ROOT_TITLE } : {}) }, 200, { 'cache-control': 'public, max-age=300' })
}
