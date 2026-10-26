/**
 * `GET /api/filter-caps` — what the map's filter accepts on this deployment, for the filter box (its inline
 * refusal and help, `_lib/indexedOnly.ts`), and the store root's label (`ROOT_LABEL`, the unfiltered view's
 * root crumb), for a page whose view was refused and so has no tree to name its root. No data, no gate:
 * deployment config only. `store=<key>` answers for that secondary store (its own `ROOT_LABEL`). A union view
 * (`_lib/union.ts`) accepts what every member accepts (indexed-only when any member is) under the deployment's
 * label, as its root is drawn.
 *
 *   → { indexedOnly: true, rootLabel: 'Marin CoreWeave', rootTitle: '…' }   (`FILTER_INDEXED_ONLY=1`: one literal
 *     substring of a name, unscoped; `rootLabel` / `rootTitle` (`ROOT_TITLE`, the root crumb's tooltip) absent when
 *     the deployment sets none; `pathTerms: true` with `SEARCH_PATH_TERMS=1`, path terms answered — absent, a term
 *     holding `/` is refused, `unsupported-slash`; `defaultScan: 'searchable'` with `DEFAULT_SCAN=searchable`: a
 *     view with no `?d=` opens on the newest scan the static name index covers, `src/defaultScan.ts` — absent,
 *     on the newest published scan)
 */
import { type Ctx, type Env, json } from '../_lib/auth.js'
import { indexedOnly, pathTerms } from '../_lib/indexedOnly.js'
import { withStore } from '../_lib/stores.js'
import { scanMode } from '../_lib/defaultScan.js'
import { memberCtxs, unionFor } from '../_lib/union.js'

const caps = (env: Env, strict: boolean, paths: boolean): Response =>
  json({ indexedOnly: strict, ...(strict && paths ? { pathTerms: true } : {}), ...(scanMode(env) === 'searchable' ? { defaultScan: 'searchable' } : {}), ...(env.ROOT_LABEL ? { rootLabel: env.ROOT_LABEL } : {}), ...(env.ROOT_TITLE ? { rootTitle: env.ROOT_TITLE } : {}) }, 200, { 'cache-control': 'public, max-age=300' })

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  if (u) {
    const members = await memberCtxs(ctx0, u)
    if (members instanceof Response) return members
    return caps(ctx0.env, members.some(m => indexedOnly(m.env)), members.every(m => pathTerms(m.env)))
  }
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  return caps(ctx.env, indexedOnly(ctx.env), pathTerms(ctx.env))
}
