/**
 * `GET /api/store` — the configured scan store, as copy: where the `/files`
 * proxy reads from (`storeTarget`) and which prefixes it exposes
 * (`storePrefixes`, the `/v1/files` defaults). No credentials, and no
 * endpoint URL either (an R2 endpoint carries the account id — the `uri` is
 * all the copy needs). The `/files` page header names it instead of a
 * baked-in bucket.
 *
 *   → { uri: "r2://idx", prefixes: ["listing/", …] }
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { withStore } from '../_lib/stores.js'
import { storePrefixes, storeScheme, storeTarget } from '../_lib/index.js'

export const FILES_PREFIXES = ['listing/', 'snapshots/', 'sweep/']

export const onRequest = async (ctx0: Ctx): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  const target = storeTarget(ctx.env)
  return json({ uri: `${storeScheme(target.endpoint)}://${target.bucket}`, prefixes: storePrefixes(ctx.env, FILES_PREFIXES) })
}
