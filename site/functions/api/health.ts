// GET /api/health — the state of the deployment's append-only stores (specs/done/health-page.md): each store's stack (base,
// runs, due carries, compaction gauge), every scan's coverage and its gaps, and freshness. Gated like /api/scan-runs
// (viewer). The document is a few R2 lists, gets and heads plus two D1 reads: held in the colo cache for
// `HEALTH_TTL` s (keyed by the store and its generations), so a page's polling costs one read a minute per colo.
// `build` (`{ sha, time }`, `_lib/build.ts`): the build serving this response, added as it is served (never cached).
import { type Env, json, requireViewer } from '../_lib/auth.js'
import { BUILD } from '../_lib/build.js'
import { health } from '../_lib/health.js'
import { ivGen, withStaticGen } from '../_lib/index.js'
import { storeKey, withStore } from '../_lib/stores.js'

/** How long a computed document stands (s). */
export const HEALTH_TTL = 60

const colo = (): Cache | null => (typeof caches === 'undefined' ? null : (caches as unknown as { default: Cache }).default)

export const onRequestGet = async (ctx0: { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  // The stores' generations, each the pin, else the store's current one, fixed for the document (and its key):
  // the interval store's (`ivGen`), the static name index's (`withStaticGen`).
  const st = await withStaticGen(ctx.env)
  const env: Env = st.PATH_STORE ? { ...st, INTERVAL_STORE_GEN: (await ivGen(st)) ?? undefined } : st
  const headers = { 'cache-control': 'private, max-age=30' }
  const fresh = new URL(ctx.request.url).searchParams.has('fresh')
  const generation = `${env.INTERVAL_STORE_PREFIX ? `${env.INTERVAL_STORE_PREFIX}:` : ''}${env.INTERVAL_STORE_GEN ?? '-'}`
  const key = new Request(`https://health.cache/${encodeURIComponent(storeKey(env))}/${encodeURIComponent(generation)}/${encodeURIComponent(env.STATIC_GEN ?? '-')}`)
  const cache = colo()
  if (cache && !fresh) {
    const hit = await cache.match(key)
    if (hit) return json({ ...await hit.json() as object, build: BUILD }, 200, { ...headers, 'x-health-cache': 'hit' })
  }
  const doc = await health(env, Math.floor(Date.now() / 1000))
  if (cache) {
    const put = cache.put(key, new Response(JSON.stringify(doc), { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${HEALTH_TTL}` } }))
    if (ctx.waitUntil) ctx.waitUntil(put)
    else await put
  }
  return json({ ...doc, build: BUILD }, 200, { ...headers, 'x-health-cache': 'miss' })
}
