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
 *  are read, together. `nc=<nonce>` (a benchmark's cold read) joins that key for an admin only: anyone else's request
 *  keys, and answers, as without it. Unscoped diffs of an interval store with churn only: anything else is a 409 (the client draws
 *  `/api/diff` then).
 *
 *  The fixed phase overlaps (`shared.ahead`): the gate (`requireViewer`) runs beside the path store's pointers, and,
 *  for a request presenting credentials (or a union member's, its gate passed), beside the scans' checks, the cache-key generations, the asked directories'
 *  cache lookups and the first round's handles: the level sort's where the generation has it, else the `path` sort's
 *  and the plan (its footer reads). Nothing is used before the gate passes;
 *  a refused request's lookups are dropped. `Server-Timing` `fixed`: request start to the first data read.
 *
 *  On a union view (`_lib/union.ts`) both cutoffs resolve as `/api/diff`'s do, and each member answers at its own
 *  scans (its own cache entries): a directory below the root by the member holding it (an unchanged member, its one
 *  scan against itself: no rows); the root by every comparable member's root, merged (`mergeLevels`: totals summed,
 *  the `k` cut after the merge), with an unchanged member's root total on both sides and a member unknown at either
 *  cutoff left out (`union.sources[].change`). A changed member that has no level (no interval store, no churn) makes
 *  the root a 409 naming it.
 */
import { type Env, type Identity, json, presentsCredentials, requireViewer } from '../../_lib/auth.js'
import { buildLevels, type Level, LEVEL_V, levelK, type Measure, mergeLevels, openLevelIndex, planLevel, prepareLevels } from '../../_lib/diffLevel.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, serverTiming } from '../../_lib/edgeCache.js'
import { DiffUnavailable, intervalsOn, ivRefusal, ivRetry, pathGens, storeReady, withPathStore } from '../../_lib/index.js'
import { isHidden, isHideRoot, restrictionFor } from '../../_lib/restrict.js'
import { scanArg } from '../../_lib/scanArg.js'
import { ahead, loadScopes, traceJoins } from '../../_lib/shared.js'
import { storeKey, withStore } from '../../_lib/stores.js'
import { cutoffOf, type Member, memberOf, memberRequest, membersAt, openUnion, UnionOverlap, unionFor } from '../../_lib/union.js'
import type { UnionView } from '../../_lib/stores.js'
import { readRootAgg } from '../../_lib/view.js'
import type { MemberChange } from '../diff.js'
import { isScanId } from '../../../src/scanSlug.js'

/** Directories one request asks for at most (the client splits a wider level). */
export const LEVEL_DIRS = 64

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  return u ? unionLevels(ctx0, u) : levelsOf(ctx0)
}

/** A union member's level request (`unionLevels`): the identity its gate already resolved, and whether its two scans
 *  may be one (a member that didn't rescan between the cutoffs). */
type MemberLevel = { id: Identity | null; same?: boolean }

/** The request's asked directories, `k` and measure, or its 400. */
function levelArgs(sp: URLSearchParams): { dirs: string[]; k: number; m: Measure } | Response {
  const dirs = [...new Set(sp.getAll('p').map(p => p.replace(/\/+$/, '')))]
  if (!dirs.length) return new Response('p=<dir> required', { status: 400 })
  if (dirs.length > LEVEL_DIRS) return new Response(`at most ${LEVEL_DIRS} dirs per request`, { status: 400 })
  if (dirs.some(p => p.includes('..') || p.startsWith('/'))) return new Response('bad path', { status: 400 })
  return { dirs, k: levelK(Number(sp.get('k')) || 32), m: sp.get('m') === 'net' ? 'net' : 'churn' }
}

async function levelsOf(ctx0: Ctx, member?: MemberLevel): Promise<Response> {
  const st = serverTiming()
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  if (!storeReady(ctx1.env)) return new Response('diff API not configured (missing index store creds)', { status: 503 })
  const sp = new URL(ctx1.request.url).searchParams
  const from = sp.get('from') ?? ''
  const to = sp.get('to') ?? ''
  if (!isScanId(from) || !isScanId(to)) return new Response('bad from/to', { status: 400 })
  if (from > to || (from === to && !member?.same)) return new Response('from must precede to', { status: 400 })
  const args = levelArgs(sp)
  if (args instanceof Response) return args
  const { dirs, k, m } = args

  // The gate, beside the path store's pointers (the identity reads neither); a union member's was passed already.
  const gate = member ? Promise.resolve(member.id) : st.time('auth', requireViewer(ctx1 as never))
  gate.catch(() => {})
  const ctx = await withPathStore(ctx1)
  traceJoins(ctx.env, st.trace)
  await loadScopes()
  const early = ahead(ctx.env, !!member || presentsCredentials(ctx1 as never))
  try {
    if (!intervalsOn(ctx.env)) return new Response('level diffs read the interval store', { status: 409 })
    // What the answer needs whoever asks: started now for a request that may pass the gate, used once it has.
    const scans = early.later(() => Promise.all(['from', 'to'].map(key => scanArg(ctx.env, sp, key))))
    const gens = early.later(() => st.time('pre', pathGens(ctx.env, [from, to])))
    const tag = cacheEnvTag(ctx.env, ctx.request)
    // A bench's cache-key nonce (`nc=`): an admin's only, so known once the gate passes; a request carrying one looks
    // its directories up then, not beside the gate.
    const nc = sp.get('nc') ?? ''
    let nonce = ''
    const keyOf = (g: string, p: string) => cacheKeyFor('level', tag, `${from}/${to}/${encodeURIComponent(p)}?k=${k}&m=${m}&g=${g}&v=${LEVEL_V}${nonce ? `&nc=${encodeURIComponent(nonce)}` : ''}`, storeKey(ctx.env))
    const lookup = (g: string, ps: string[]) => st.time('match', Promise.all(ps.map(async p => [p, await cacheMatch(ctx.env, keyOf(g, p))] as const)))
    const asked = nc ? null : early.later(async () => lookup(await gens(), dirs))
    // The first round's handles, beside the lookups: the level sort's (its tiers' footers) where the generation has
    // it; only where it hasn't, the `path` sort's and the plan (its footer reads). A read of every asked directory
    // takes them as they are; a read of fewer plans again over the footers they left held. A failed open or plan here
    // is no answer: the read opens or plans again and fails as it would have.
    const lvl = early.later(() => openLevelIndex(ctx.env, { from, to }).catch(() => undefined))
    const prep = early.later(async () => ((await lvl()) === null ? prepareLevels(ctx.env, { from, to, trace: st.trace }) : undefined))
    const plan = early.later(() => prep().then(p => p && planLevel(p, dirs)).catch(() => undefined))

    const gated = await gate
    if (gated instanceof Response) return gated
    early.commit()
    if (gated?.admin) nonce = nc
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
      let hits = asked ? await asked() : await lookup(g, dirs)
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
          const given = first ? { level: await lvl(), prep: await prep(), plan: sameSet(todo, dirs) ? await plan() : undefined } : undefined
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
    // A union member's request is its siblings' too: its unused lookups run out rather than cancel theirs.
    early.done(!member)
  }
}

/** Whether `a` and `b` hold the same directories. */
const sameSet = (a: string[], b: string[]): boolean => a.length === b.length && a.every(x => b.includes(x))

type LevelBody = { from: string; to: string; k: number; m: Measure; levels: Level[] }

/** `/api/diff/level` on a union view (see the module doc). */
async function unionLevels(ctx0: Ctx, view: UnionView): Promise<Response> {
  const sp = new URL(ctx0.request.url).searchParams
  const args = levelArgs(sp)
  if (args instanceof Response) return args
  const { dirs, k, m } = args
  try {
    const un = await openUnion(ctx0, view, true)
    if (un instanceof Response) return un
    const cf = await cutoffOf(un, sp, 'from')
    if (cf instanceof Response) return cf
    const ct = await cutoffOf(un, sp, 'to')
    if (ct instanceof Response) return ct
    if (cf.at >= ct.at) return new Response('from must precede to', { status: 400 })
    const [rf, rt] = [membersAt(un, cf), membersAt(un, ct)]
    const pairs = view.members.map((store, i) => {
      const from = rf.members[i].date, to = rt.members[i].date
      const change: MemberChange = from && to ? (from === to ? 'unchanged' : 'changed') : 'unknown'
      return { store, env: rt.members[i].ctx.env, from, to, change }
    })
    const sources = pairs.map(({ store, from, to, change }) => ({ store, from, to, change }))
    /** A member's levels of `ps` at its own scans: its answer, or its refusal as is. */
    const ask = async (p: typeof pairs[number], ps: string[]): Promise<LevelBody | Response> => {
      const req = memberRequest(ctx0.request, p.store, { from: p.from!, to: p.to! }, ['p'])
      const url = new URL(req.url)
      for (const d of ps) url.searchParams.append('p', d)
      const r = await levelsOf({ ...ctx0, request: new Request(url, req) }, { id: un.id, same: p.change === 'unchanged' })
      return r.ok ? await r.json() as LevelBody : r
    }
    // Below the root: each directory's member, asked once for all of its directories.
    const byStore = new Map<string, string[]>()
    const owner = new Map<string, Promise<Member | null>>()
    const ownerOf = (seg: string) => owner.get(seg) ?? owner.set(seg, (async () => await memberOf(rt, seg) ?? await memberOf(rf, seg))()).get(seg)!
    for (const d of dirs.filter(d => d !== '')) {
      const mem = await ownerOf(d.split('/')[0])
      if (!mem) return new Response('path not found in either scan', { status: 404 })
      const p = pairs[view.members.indexOf(mem.store)]
      if (p.change === 'unknown') return json({ error: `store '${p.store}' has no scan by ${p.from ? ct.date : cf.date}: its change is unknown`, union: { view: view.key, sources } }, 409)
      byStore.set(p.store, [...byStore.get(p.store) ?? [], d])
    }
    const out = new Map<string, Level>()
    const asked = await Promise.all([...byStore].map(([store, ps]) => ask(pairs.find(p => p.store === store)!, ps)))
    const refused = asked.find(a => a instanceof Response)
    if (refused) return refused as Response
    for (const b of asked as LevelBody[]) for (const l of b.levels) out.set(l.path, l)
    if (dirs.includes('')) {
      const comparable = pairs.filter(p => p.change !== 'unknown')
      const roots = await Promise.all(comparable.map(async (p): Promise<LevelBody | Response | Level> => {
        if (p.change === 'changed') return ask(p, [''])
        // An unchanged member: its root total on both sides, nothing changed (whatever store it is).
        const r = await readRootAgg(p.env, { date: p.from!, path: '' })
        if (!r) return new Response(`its root at ${p.from} has no total (an indexed scan with no root rows)`, { status: 500 })
        const t = { a: r.b, b: r.b, oa: r.o, ob: r.o }
        return { path: '', ...t, churn: 0, rows: [], other: null, unchanged: t }
      }))
      const bad = roots.findIndex(r => r instanceof Response)
      if (bad >= 0) {
        const r = roots[bad] as Response
        return new Response(`level diffs are not available on union view '${view.key}''s root: store '${comparable[bad].store}': ${await r.text()}`, { status: r.status })
      }
      const ok = roots as (LevelBody | Level)[]
      const parts = ok.map(r => ('levels' in r ? r.levels.find(l => l.path === '')! : r))
      const root = mergeLevels('', parts, k, m)
      out.set('', root)
      // The root's single changed directory: its member followed the same chain (it had that one change alone).
      const chained = new Map(ok.flatMap(r => ('levels' in r ? r.levels.filter(l => l.path !== '').map(l => [l.path, l] as const) : [])))
      for (let n = root.next; n && !out.has(n) && chained.has(n); n = chained.get(n)!.next) out.set(n, chained.get(n)!)
    }
    return new Response(JSON.stringify({ from: cf.date, to: ct.date, k, m, levels: [...out.values()], union: { view: view.key, sources } }), {
      headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=300' },
    })
  } catch (e) {
    const refused = ivRefusal(e)
    if (refused) return refused
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`level diff failed: ${msg}`, { status: 500 })
  }
}
