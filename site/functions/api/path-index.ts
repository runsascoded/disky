// Ranged, auth-gated access to the floor-free path index — the raw rows
// behind /api/subtree, for people who'd rather bring their own query engine:
//
//   HEAD /api/path-index?date=YYYY-MM-DD          → size + Accept-Ranges
//   GET  /api/path-index?date=YYYY-MM-DD          → 200, requires Range (see below)
//        (with `Range: bytes=a-b`)                → 206 + Content-Range
//
// DuckDB reads it directly (httpfs sends HEAD, then range GETs — footer,
// row-group stats, then only the pages a predicate needs):
//
//   CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {
//     'Authorization': 'Bearer <your /api/token>' });
//   SELECT * FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
//   WHERE depth = 2 ORDER BY b DESC LIMIT 20;
//
// Schema: one row per rolled-up path × attribution slice —
// (path, depth, usr, b, o, wts, wb, c2, c3, c4, a), sorted (depth, path).
//
// A Workers isolate buffers each range in memory, so ranges are capped; a
// bare GET would mean buffering the whole multi-GB file and is refused with
// a pointer to Range requests instead.
import { S3Store } from '@rdub/file-tree/stores/s3'
import { withStore } from '../_lib/stores.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { indexDir, storeCreds, storePrefixes, storeReady, storeTarget } from '../_lib/index.js'

const MAX_RANGE = 64 * 1024 * 1024 // 64MB per request — plenty for parquet pages

export const onRequest = async (ctx0: { request: Request; env: Env }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const { request, env } = ctx
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return new Response('method not allowed', { status: 405, headers: { allow: 'GET, HEAD' } })
  }
  if (!storeReady(env)) {
    return new Response('path-index proxy not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(request.url)
  const date = url.searchParams.get('date') ?? ''
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  // The GCS index carries no CW data today, but keep the gate shape ready for
  // a `store=cw` variant; base access = the same `gcs` scope as the app.
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated

  const store = S3Store({ ...storeTarget(env), prefixes: storePrefixes(env, ['listing/']), ...storeCreds(env) })
  // The file lives under the generation dir D1 points at (a run never
  // overwrites the file being served; specs/view-serving.md).
  const dir = await indexDir(env, date)
  if (!dir) return new Response(`no path index synced for ${date}`, { status: 404 })
  const key = `${dir}/path-index.parquet`

  // Size via a 1-byte ranged probe's Content-Range (as /api/subtree does).
  let size: number
  try {
    const probe = await store.get(key, { offset: 0, length: 1 })
    if (!probe.totalSize) throw new Error('no Content-Range')
    size = probe.totalSize
  } catch {
    return new Response('no path index for that date', { status: 404 })
  }

  const base = {
    'accept-ranges': 'bytes',
    'content-type': 'application/octet-stream',
    'cache-control': 'private, max-age=86400', // immutable per date
  }
  if (request.method === 'HEAD') {
    return new Response(null, { headers: { ...base, 'content-length': String(size) } })
  }

  const range = request.headers.get('range')
  if (!range) {
    return new Response(
      `this file is ${size} bytes — use Range requests (DuckDB/pyarrow/hyparquet do automatically)\n`,
      { status: 416, headers: { ...base, 'content-range': `bytes */${size}` } },
    )
  }
  const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim())
  if (!m || (m[1] === '' && m[2] === '')) {
    return new Response('unsupported Range (single bytes=a-b span only)', { status: 416 })
  }
  // bytes=a-b (inclusive), bytes=a- (to end), bytes=-n (final n bytes — how
  // parquet readers grab the footer).
  const start = m[1] === '' ? Math.max(0, size - Number(m[2])) : Number(m[1])
  const endIncl = m[1] === '' ? size - 1 : m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1)
  if (start >= size || endIncl < start) {
    return new Response('range out of bounds', { status: 416, headers: { 'content-range': `bytes */${size}` } })
  }
  const length = endIncl - start + 1
  if (length > MAX_RANGE) {
    // Serving fewer bytes than asked is technically legal 206 but trips up
    // clients that assume the full span — refuse loudly instead. Parquet
    // readers never need spans this big (row groups here are a few MB).
    return new Response(`range too large (${length} bytes; max ${MAX_RANGE})`, { status: 416 })
  }
  const { bytes } = await store.get(key, { offset: start, length })
  return new Response(bytes, {
    status: 206,
    headers: {
      ...base,
      'content-length': String(bytes.byteLength),
      'content-range': `bytes ${start}-${start + bytes.byteLength - 1}/${size}`,
    },
  })
}
