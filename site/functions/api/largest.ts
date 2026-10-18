/** The largest files anywhere under a path — a flat, recursive "Large Files"
 * list (objects only, never dirs), size-descending, from the store
 * generation's `bysize` sort (`readLargest` in `_lib/index.ts`); with
 * `kind=dir`, the largest folders strictly below it instead (never objects,
 * never the path itself).
 *
 *   GET /api/largest?date=<scan>|d=<slug>&path=<P>[&n=<1..500>][&kind=file|dir][&store=<key>]
 *
 * → `{ date, path, n, files: [{ path, size, mtime }], more, groups, rows, capped? }`
 * (`kind=dir`: `{ date, path, n, kind: 'dir', dirs: [{ path, size, mtime, n_files }], … }`,
 * `n_files` = the objects below each): `path` is the full store path (no
 * scheme), `mtime` epoch seconds (null where the store has none; a folder's
 * newest), ties broken by path. `more`: files beyond the `n` listed
 * exist, or may (groups left unread). `groups` / `rows`: the size-sort row
 * groups decoded and the rows they held — the read's cost. `capped`: the read
 * hit its group budget, so the list is the largest in what it read.
 *
 * Gated like `/api/subtree` (viewer scope); immutable per (scan, path, n,
 * generation, kind), so edge-cached like it. A version-1 scan (dirs only, no size
 * sort) is a 409. Reads the request's path store (`withPathStore`): under
 * `PATH_STORE=intervals` a scan the interval store holds is read from its
 * `bysize` sort as of that scan (`readLargest` → `readLargestIv`: its runs' close
 * records applied, exactly the scan's own answer), any other from the scan's own
 * size sort — never another scan's files. A scan the store holds never reads its
 * per-scan sort (retired once the store holds it): a scan a broken run cut out is a
 * 503 + `Retry-After` (`ivRefusal`).
 */
import { filterJson, hiddenNotFound, isHidden, restrictionFor } from '../_lib/restrict.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { ivRefusal, ivRetry, type LargestKind, openIndex, pathGens, readLargest, sizeVariant, storeReady, withPathStore } from '../_lib/index.js'
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
  const kindRaw = url.searchParams.get('kind') ?? 'file'
  if (kindRaw !== 'file' && kindRaw !== 'dir') return new Response('bad kind (want file|dir)', { status: 400 })
  const kind: LargestKind = kindRaw
  const key = kind === 'dir' ? 'dirs' : 'files'

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  // A path-restricted link (specs/path-scopes.md): a hidden path is absent and
  // hidden files drop out of the (cached, unrestricted) list on the way out —
  // the list can then hold fewer than `n`.
  const restriction = restrictionFor(gated, ctx.env)
  if (restriction && isHidden(path, restriction)) return hiddenNotFound()
  const out = (res: Response | Promise<Response>) =>
    restriction ? Promise.resolve(res).then(r => filterJson(r, (b: Record<string, { path: string }[]>) => ({ ...b, [key]: b[key].filter(f => !isHidden(f.path, restriction)) }))) : res

  try {
    // An unindexed id, or a `d=` slug matching no scan: a 404, never another scan.
    const scan = await scanArg(ctx.env, url.searchParams)
    if (scan instanceof Response) return scan
    const date = scan
    const env = ctx.env
    const g = await st.time('pre', pathGens(env, [date]))
    const cacheKey = cacheKeyFor('largest', cacheEnvTag(ctx.env, ctx.request), `${date}/${encodeURIComponent(path)}?n=${n}${kind === 'dir' ? '&k=dir' : ''}&g=${g}`, storeKey(ctx.env))
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return out(hit)
    // A broken interval-store run is cut out and the read tried again: a scan before it answers, the run's own
    // scans are a 503 + `Retry-After` (`ivRefusal`), never a per-scan read.
    let r
    try {
      r = await ivRetry(async () => readLargest({ ...(await openIndex(env, date, sizeVariant('path'))), trace: st.trace }, path, n, undefined, kind))
    } catch (e) {
      if (String((e as Error).message).includes('not synced')) return new Response('no size sort for this scan (objects not indexed)', { status: 409 })
      throw e
    }
    const body = JSON.stringify({
      date,
      path,
      n,
      ...(kind === 'dir'
        ? { kind, dirs: r.rows.map(x => ({ path: x.path, size: x.size, mtime: x.mtime, n_files: x.n_files })) }
        : { files: r.rows.map(x => ({ path: x.path, size: x.size, mtime: x.mtime })) }),
      more: r.more,
      groups: r.groups.length,
      rows: r.decoded,
      ...(r.capped ? { capped: true } : {}),
    })
    return await out(cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx)))
  } catch (e) {
    const refused = ivRefusal(e)
    if (refused) return refused
    const msg = String((e as Error).message ?? e)
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`largest failed: ${msg}`, { status: 500 })
  }
}
