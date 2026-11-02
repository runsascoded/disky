/** A progressive diff's level request (specs/diff-progressive.md §1, `_lib/diffLevel.ts`): each asked directory's
 *  changed children between two scans, sized by range churn.
 *
 *   GET /api/diff/level?from=<scan>&to=<scan>&p=<dir>[&p=<dir>…][&k=<n>][&m=churn|net]
 *
 *  `p` (repeated, at most `LEVEL_DIRS`): the directories, usually one depth's; `''` (a bare `p=`) is the root. `k`: the
 *  children drawn per directory (snapped to a power of two, `levelK`); `m`: what ranks and sizes them. Answers
 *  `{ from, to, k, m, levels: Level[] }`, the asked directories' and those their single-changed-directory chains lead
 *  into (`Level.next`). Each directory's answer is its own edge-cache entry (keyed by the store generation, both
 *  scans, the directory, `k` and `m`, never the canvas), so a later batch, view or drill reuses it; only the misses
 *  are read, together. Unscoped diffs of an interval store with churn only: anything else is a 409 (the client draws
 *  `/api/diff` then).
 */
import { type Env, requireViewer } from '../../_lib/auth.js'
import { buildLevels, type Level, LEVEL_V, levelK, type Measure } from '../../_lib/diffLevel.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../../_lib/edgeCache.js'
import { DiffUnavailable, intervalsOn, ivRefusal, ivRetry, pathGens, storeReady, withPathStore } from '../../_lib/index.js'
import { isHidden, isHideRoot, restrictionFor } from '../../_lib/restrict.js'
import { scanArg } from '../../_lib/scanArg.js'
import { traceJoins } from '../../_lib/shared.js'
import { storeKey, withStore } from '../../_lib/stores.js'
import { unionFor } from '../../_lib/union.js'
import { isScanId } from '../../../src/scanSlug.js'

/** Directories one request asks for at most (the client splits a wider level). */
export const LEVEL_DIRS = 64

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  if (u) return new Response('level diffs are not available on a union view', { status: 409 })
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = await withPathStore(ctx1)
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) return new Response('diff API not configured (missing index store creds)', { status: 503 })
  const url = new URL(ctx.request.url)
  const sp = url.searchParams
  const from = sp.get('from') ?? ''
  const to = sp.get('to') ?? ''
  const dirs = [...new Set(sp.getAll('p').map(p => p.replace(/\/+$/, '')))]
  const k = levelK(Number(sp.get('k')) || 32)
  const m: Measure = sp.get('m') === 'net' ? 'net' : 'churn'
  if (!isScanId(from) || !isScanId(to)) return new Response('bad from/to', { status: 400 })
  if (from >= to) return new Response('from must precede to', { status: 400 })
  if (!dirs.length) return new Response('p=<dir> required', { status: 400 })
  if (dirs.length > LEVEL_DIRS) return new Response(`at most ${LEVEL_DIRS} dirs per request`, { status: 400 })
  if (dirs.some(p => p.includes('..') || p.startsWith('/'))) return new Response('bad path', { status: 400 })
  if (!intervalsOn(ctx.env)) return new Response('level diffs read the interval store', { status: 409 })

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  // A path-restricted link: a hidden directory (or one shown as a leaf) has no level; nothing under a visible one is
  // hidden.
  const restriction = restrictionFor(gated, ctx.env)
  const shut = (p: string) => !!restriction && (isHidden(p, restriction) || isHideRoot(p, restriction))
  if (dirs.some(shut)) return new Response('path not found', { status: 404 })
  const miss = (await Promise.all(['from', 'to'].map(key => scanArg(ctx.env, sp, key)))).find(r => r instanceof Response)
  if (miss) return miss

  try {
    const g = await st.time('pre', pathGens(ctx.env, [from, to]))
    const tag = cacheEnvTag(ctx.env, ctx.request)
    const keyOf = (p: string) => cacheKeyFor('level', tag, `${from}/${to}/${encodeURIComponent(p)}?k=${k}&m=${m}&g=${g}&v=${LEVEL_V}`, storeKey(ctx.env))
    const out = new Map<string, Level>()
    // Cached answers, and the chains they lead into, a link at a time.
    let look = dirs
    const todo: string[] = []
    while (look.length) {
      const hits = await st.time('match', Promise.all(look.map(async p => [p, await cacheMatch(ctx.env, keyOf(p))] as const)))
      const next: string[] = []
      for (const [p, hit] of hits) {
        if (!hit) { todo.push(p); continue }
        const l = await hit.json<Level>()
        out.set(p, l)
        if (l.next && !out.has(l.next) && !shut(l.next)) next.push(l.next)
      }
      look = next
    }
    const cached = out.size
    st.trace('level-hits', cached, `${cached} cached, ${todo.length} read`)
    if (todo.length) {
      const built = await ivRetry(() => buildLevels(ctx.env, { from, to, dirs: todo, k, m, trace: st.trace }))
      for (const [p, l] of built) {
        if (shut(p)) continue
        out.set(p, l)
        await cacheStore(ctx.env, keyOf(p), JSON.stringify(l), {}, ctx.waitUntil?.bind(ctx))
      }
    }
    const levels = [...out.values()].map(l => (l.next && shut(l.next) ? { ...l, next: undefined } : l))
    return new Response(JSON.stringify({ from, to, k, m, levels }), {
      headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=300', 'server-timing': st.header(), 'x-cache': !todo.length ? 'hit' : cached ? 'partial' : 'miss' },
    })
  } catch (e) {
    const refused = ivRefusal(e)
    if (refused) return refused
    if (e instanceof DiffUnavailable) return new Response(e.message, { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`level diff failed: ${msg}`, { status: 500 })
  }
}
