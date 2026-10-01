// CF Pages Function: read-only proxy for the *scanned* buckets' objects, so the
// map's object panel can preview a file (parquet, CSV, JSON, text, images, …)
// that lives in a bucket the deployment scans rather than in its own scan
// store. `/v1/objects/<bucket>/<file-tree route>`, e.g.
// `/v1/objects/marin-us-east1/get?path=<key>` with HTTP Range.
//
// Only buckets named in `OBJECT_BUCKETS` (comma-separated) are served, whole
// bucket (no prefix allow-list: these are the data the map already lists by
// name and size). Reads use the scan-store creds (`storeCreds`), so the
// deployment grants that key read on each listed bucket.
//
// Gate: the full base scope — a signed-in member. A read-only guest (a share
// link's `<base>:read`) can browse the map but not open scanned objects, and a
// public deploy's anonymous identity never can, whatever `PUBLIC_READ` says.
import { createHandlers } from '@rdub/file-tree/server'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type Env, json, requireStager } from '../../_lib/auth.js'
import { storeCreds, storeReady, storeTarget } from '../../_lib/index.js'

const BASE = '/v1/objects'

/** The buckets this deployment's object proxy serves (`OBJECT_BUCKETS`). */
export const objectBuckets = (env: Env): string[] =>
  (env.OBJECT_BUCKETS ?? '').split(',').map(s => s.trim()).filter(Boolean)

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const id = await requireStager(ctx)
  if (id instanceof Response) return id
  if (id.via === 'public') return json({ error: 'unauthenticated' }, 401)
  const bucket = new URL(ctx.request.url).pathname.slice(BASE.length + 1).split('/')[0]
  if (!bucket || !objectBuckets(ctx.env).includes(bucket)) return json({ error: 'no such object bucket' }, 404)
  if (!storeReady(ctx.env)) {
    return new Response('object proxy not configured (missing store creds)', { status: 503 })
  }
  const store = S3Store({ ...storeTarget(ctx.env), bucket, ...storeCreds(ctx.env) })
  const handlers = createHandlers(store, { basePath: `${BASE}/${bucket}`, corsOrigin: null })
  return (await handlers.handle(ctx.request)) ?? new Response('not found', { status: 404 })
}
