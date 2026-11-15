/** The store's non-live bytes over time (specs/non-live-bytes.md): the scan step `non-live`'s metric history
 * (`options.non-live.source: metric`, `dt-cloud non-live -S`), one file rewritten each scan, read through the served seam.
 *
 *   GET /api/non-live → {series: {as_of, metric, dates: [<day>…], buckets: {<bucket>: {<class>: [bytes | null, …]}}} | null}   viewer
 *
 * Bucket totals per UTC day (`live` beside `soft-deleted`, `noncurrent`, `mpu`; a class 0 every day absent, null a day the
 * metric had nothing for the bucket). Null: no history (the step reads listings here, or hasn't run). Whole-bucket sums,
 * so a path-restricted link is refused (`RESTRICTED_BLOCKED`): they count whatever it hides. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { servedStore, storeReady } from '../_lib/index.js'

export const NON_LIVE_PREFIX = 'snapshots/non-live/'
export const NON_LIVE_SERIES = `${NON_LIVE_PREFIX}series.json`

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const { env } = ctx
  let series: unknown = null
  if (env.SERVED_R2 ? env.INDEX_R2 : storeReady(env)) {
    try {
      series = JSON.parse(new TextDecoder().decode((await servedStore(env, [NON_LIVE_PREFIX]).get(NON_LIVE_SERIES)).bytes))
    } catch (error) {
      console.log(`non-live series: ${(error as Error).message}`)
    }
  }
  return json({ series }, 200, { 'cache-control': 'private, max-age=600' })
}
