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
 *
 *  The fixed phase overlaps (`shared.ahead`): the gate (`requireViewer`) runs beside the path store's pointers, and,
 *  for a request presenting credentials, beside the scans' checks, the cache-key generations, the asked directories'
 *  cache lookups and the first round's handles and plan (its footer reads). Nothing is used before the gate passes;
 *  a refused request's lookups are dropped. `Server-Timing` `fixed`: request start to the first data read.
 */
import { type Env, presentsCredentials, requireViewer } from '../../_lib/auth.js'
import { buildLevels, type Level, LEVEL_V, levelK, type Measure, planLevel, prepareLevels } from '../../_lib/diffLevel.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../../_lib/edgeCache.js'
import { DiffUnavailable, intervalsOn, ivRefusal, ivRetry, pathGens, storeReady, withPathStore } from '../../_lib/index.js'
import { isHidden, isHideRoot, restrictionFor } from '../../_lib/restrict.js'
import { scanArg } from '../../_lib/scanArg.js'
import { ahead, traceJoins } from '../../_lib/shared.js'
import { storeKey, withStore } from '../../_lib/stores.js'
import { unionFor } from '../../_lib/union.js'
import { isScanId } from '../../../src/scanSlug.js'

/** Directories one request asks for at most (the client splits a wider level). */
export const LEVEL_DIRS = 64

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const st = serverTiming()
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  if (u) return new Response('level diffs are not available on a union view', { status: 409 })
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  if (!storeReady(ctx1.env)) return new Response('diff API not configured (missing index store creds)', { status: 503 })
  const sp = new URL(ctx1.request.url).searchParams
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

  // The gate, beside the path store's pointers (the identity reads neither).
  const gate = st.time('auth', requireViewer(ctx1 as never))
  gate.catch(() => {})
  const ctx = await withPathStore(ctx1)
  traceJoins(ctx.env, st.trace)
  const early = ahead(ctx.env, presentsCredentials(ctx1 as never))
  try {
    if (!intervalsOn(ctx.env)) return new Response('level diffs read the interval store', { status: 409 })
    // What the answer needs whoever asks: started now for a request that may pass the gate, used once it has.
    const scans = early.later(() => Promise.all(['from', 'to'].map(key => scanArg(ctx.env, sp, key))))
    const gens = early.later(() => st.time('pre', pathGens(ctx.env, [from, to])))
    const tag = cacheEnvTag(ctx.env, ctx.request)
    const keyOf = (g: string, p: string) => cacheKeyFor('level', tag, `${from}/${to}/${encodeURIComponent(p)}?k=${k}&m=${m}&g=${g}&v=${LEVEL_V}`, storeKey(ctx.env))
    const lookup = (g: string, ps: string[]) => st.time('match', Promise.all(ps.map(async p => [p, await cacheMatch(ctx.env, keyOf(g, p))] as const)))
    const asked = early.later(async () => lookup(await gens(), dirs))
    // The first round's handles and plan (its footer reads), beside the lookups: a read of every asked directory
    // takes them as they are; a read of fewer plans again over the footers they left held.
    const prep = early.later(() => prepareLevels(ctx.env, { from, to, trace: st.trace }))
    // A failed plan here is no answer: the read plans again and fails as it would have.
    const plan = early.later(() => prep().then(p => planLevel(p, dirs)).catch(() => undefined))

    const gated = await gate
    if (gated instanceof Response) return gated
    // A path-restricted link: a hidden directory (or one shown as a leaf) has no level; nothing under a visible one is
    // hidden.
    const restriction = restrictionFor(gated, ctx.env)
    const shut = (p: string) => !!restriction && (isHidden(p, restriction) || isHideRoot(p, restriction))
    if (dirs.some(shut)) return new Response('path not found', { status: 404 })
    const miss = (await scans()).find(r => r instanceof Response)
    if (miss) return miss

    try {
      const g = await gens()
      const out = new Map<string, Level>()
      // Cached answers, and the chains they lead into, a link at a time.
      let hits = await asked()
      const todo: string[] = []
      while (hits.length) {
        const next: string[] = []
        for (const [p, hit] of hits) {
          if (!hit) { todo.push(p); continue }
          const l = await hit.json<Level>()
          out.set(p, l)
          if (l.next && !out.has(l.next) && !shut(l.next)) next.push(l.next)
        }
        hits = next.length ? await lookup(g, next) : []
      }
      const cached = out.size
      st.trace('level-hits', cached, `${cached} cached, ${todo.length} read`)
      if (todo.length) {
        // The handles and plan made beside the lookups serve the first attempt; a retry (a broken run cut out) opens afresh.
        let first = true
        const built = await ivRetry(async () => {
          const given = first ? { prep: await prep(), plan: sameSet(todo, dirs) ? await plan() : undefined } : undefined
          first = false
          return buildLevels(ctx.env, { from, to, dirs: todo, k, m, trace: st.trace, onRead: () => st.mark('fixed') }, given)
        })
        for (const [p, l] of built) {
          if (shut(p)) continue
          out.set(p, l)
          await cacheStore(ctx.env, keyOf(g, p), JSON.stringify(l), {}, ctx.waitUntil?.bind(ctx))
        }
      }
      const levels = [...out.values()].map(l => (l.next && shut(l.next) ? { ...l, next: undefined } : l))
      st.mark('fixed')
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
  } finally {
    early.done()
  }
}

/** Whether `a` and `b` hold the same directories. */
const sameSet = (a: string[], b: string[]): boolean => a.length === b.length && a.every(x => b.includes(x))
