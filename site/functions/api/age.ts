/** Per-path created-day strata for the `AgeChart` — the path-aware replacement
 * for the whole-fleet `age.json` (specs/age-index.md).
 *
 *   GET /api/age?date=<scan>&path=<prefix>
 *
 * `path` is a bucket-prefixed prefix (`marin-us-east-02a/marin/tmp/ttl=14d`),
 * or empty for the fleet root; the `age` index carries one descendant-inclusive
 * row per `(path, created-day)`, sorted `(depth, path, day)`, so a path's
 * strata are a single point lookup on its own day rows (the root is the
 * synthetic depth-0 row). Returns `{ rows: [{ d, b, o }] }` (day-granular; the
 * FE rolls to week/month) — a prefix below the index floor returns no rows and
 * the chart notes it's too small to stratify.
 */
import { type Env, requireViewer } from '../_lib/auth.js'
import { withStore } from '../_lib/stores.js'
import { num, openIndex, readPoint, storeReady } from '../_lib/index.js'

export const onRequestGet = async (ctx0: { request: Request; env: Env }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  if (!storeReady(ctx.env)) {
    return new Response('age API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const date = url.searchParams.get('date') ?? ''
  // Bucket-prefixed prefix, no trailing slash; '' = the fleet root (depth 0).
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated

  // depth 1 = a bucket (`index.py` bucket-prefixes + `depth + 1`); the fleet
  // root is depth 0. The row sort is (depth, path, day), so (depth, path) is
  // the point-lookup key.
  const depth = path === '' ? 0 : path.split('/').length
  let rows: Record<string, unknown>[]
  try {
    const h = await openIndex(ctx.env, date, 'age')
    rows = await readPoint(h, depth, path, ['path', 'depth', 'day', 'b', 'o'])
  } catch (e) {
    // Not synced for this date (no age index yet, e.g. pre-backfill): an empty
    // chart, not a 500 — the FE shows the skeleton then nothing.
    const msg = (e as Error).message
    if (/not synced/.test(msg)) return json({ rows: [] })
    throw e
  }
  const out = rows
    .map(r => ({ d: num(r.day), b: num(r.b), o: num(r.o) }))
    .sort((a, b) => a.d - b.d)
  return json({ rows: out })
}

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=86400' }, // immutable per (date, path)
  })
