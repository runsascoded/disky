/** The largest files anywhere under a path — a flat, recursive "Large Files"
 * list (objects only, never dirs), size-descending, from the store
 * generation's `bysize` sort (`readLargest` in `_lib/index.ts`).
 *
 *   GET /api/largest?date=<scan>|d=<slug>&path=<P>[&n=<1..500>][&store=<key>]
 *
 * → `{ date, path, n, files: [{ path, size, mtime }], more, groups, rows, capped? }`:
 * `path` is the full store path (no scheme), `mtime` epoch seconds (null where
 * the store has none), ties broken by path. `more`: files beyond the `n` listed
 * exist, or may (groups left unread). `groups` / `rows`: the size-sort row
 * groups decoded and the rows they held — the read's cost. `capped`: the read
 * hit its group budget, so the list is the largest in what it read.
 *
 * Gated like `/api/subtree` (viewer scope); immutable per (scan, path, n,
 * generation), so edge-cached like it. A version-1 scan (dirs only, no size
 * sort) is a 409. Reads the request's path store (`withPathStore`): under
 * `PATH_STORE=intervals` a scan the interval store holds is read from its
 * `bysize` sort as of that scan (`readLargest` → `readLargestIv`: its runs' close
 * records applied, exactly the scan's own answer), any other from the scan's own
 * size sort — never another scan's files.
 */
import { filterJson, hiddenNotFound, isHidden, restrictionFor } from '../_lib/restrict.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { openIndex, pathGens, readLargest, sizeVariant, storeReady, withPathStore } from '../_lib/index.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../_lib/edgeCache.js'
import { storeKey, withStore } from '../_lib/stores.js'
import { scanArg } from '../_lib/scanArg.js'
import { isScanId } from '../../src/scanSlug.js'

export const LARGEST_DEFAULT = 100
export const LARGEST_MAX = 500

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is); then its path store.
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = await withPathStore(ctx1)
  const st = serverTiming()
  if (!storeReady(ctx.env)) {
    return new Response('largest API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!isScanId(url.searchParams.get('date')) && !url.searchParams.has('d')) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const nRaw = url.searchParams.get('n')
  const n = nRaw == null ? LARGEST_DEFAULT : Number(nRaw)
  if (!Number.isInteger(n) || n < 1 || n > LARGEST_MAX) return new Response(`bad n (want 1..${LARGEST_MAX})`, { status: 400 })

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  // A path-restricted link (specs/path-scopes.md): a hidden path is absent and
  // hidden files drop out of the (cached, unrestricted) list on the way out —
  // the list can then hold fewer than `n`.
  const restriction = restrictionFor(gated, ctx.env)
  if (restriction && isHidden(path, restriction)) return hiddenNotFound()
  const out = (res: Response | Promise<Response>) =>
    restriction ? Promise.resolve(res).then(r => filterJson(r, (b: { files: { path: string }[] }) => ({ ...b, files: b.files.filter(f => !isHidden(f.path, restriction)) }))) : res

  try {
    // An unindexed id, or a `d=` slug matching no scan: a 404, never another scan.
    const scan = await scanArg(ctx.env, url.searchParams)
    if (scan instanceof Response) return scan
    const date = scan
    const env = ctx.env
    const g = await st.time('pre', pathGens(env, [date]))
    const cacheKey = cacheKeyFor('largest', cacheEnvTag(ctx.env, ctx.request), `${date}/${encodeURIComponent(path)}?n=${n}&g=${g}`, storeKey(ctx.env))
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return out(hit)
    let h
    try {
      h = await openIndex(env, date, sizeVariant('path'))
    } catch (e) {
      if (String((e as Error).message).includes('not synced')) return new Response('no size sort for this scan (objects not indexed)', { status: 409 })
      throw e
    }
    const r = await readLargest({ ...h, trace: st.trace }, path, n)
    const body = JSON.stringify({
      date,
      path,
      n,
      files: r.rows.map(x => ({ path: x.path, size: x.size, mtime: x.mtime })),
      more: r.more,
      groups: r.groups.length,
      rows: r.decoded,
      ...(r.capped ? { capped: true } : {}),
    })
    return await out(cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx)))
  } catch (e) {
    const msg = String((e as Error).message ?? e)
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`largest failed: ${msg}`, { status: 500 })
  }
}
