/** What changed under a path between two scans, read server-side from both
 * scans' index tiers at one shared byte floor (`_lib/view.ts` `buildDiff`).
 *
 *   GET /api/diff?from=<scan>&to=<scan>&path=<P>&w=<px>&h=<px>[&minArea=<px²>][&top=<n>]
 *                 [&lens=user:<id>|user:me][&o=owned|unowned][&q=<name filter>][&summary=1][&depth=<levels>]
 *
 * `summary=1` answers with the totals only (both sides' scoped root reads,
 * no walk — `rows` empty): the section's headline, seconds before the rows.
 *
 * Same scope axes as `/api/subtree`; the response is the treemap's row list
 * (`{ rows, total_a, total_b, objects_a, objects_b, expansions, truncated }`),
 * immutable per (from, to, path, budget, scope) plus the ledger head under a
 * user lens, and edge-cached accordingly.
 */
import { filterJson, hiddenNotFound, isHidden, restrictDiff, restrictedForbidden, restrictionFor } from '../_lib/restrict.js'
import { type Env, type Identity, json, presentsCredentials, requireViewer } from '../_lib/auth.js'
import { DESCENT_V } from '../_lib/diffDescent.js'
import { DiffUnavailable, intervalsOn, ivRefusal, ivRetry, pathGens, withPathStore, storeReady, type Lens } from '../_lib/index.js'
import { hasLedger, ledgerHead } from '../_lib/ledger.js'
import { classKey, parseClasses, parseOwner, queryParam, QueryError } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildDiff, type DiffRow, FILTER_VIEW_V, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT, readRootAgg, scopedTag } from '../_lib/view.js'
import { hexNote, hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, pathTerms, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, isPartial, keepFor, serverTiming, UPGRADE_PHASE2_MS, UPGRADE_SEARCH_LIMITS, UPGRADE_WALK_MS, upgradePartial } from '../_lib/edgeCache.js'
import type { SearchLimits } from '../_lib/search.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId } from '../../src/scanSlug.js'
import { scanArg } from '../_lib/scanArg.js'
import { ahead, loadScopes, traceJoins } from '../_lib/shared.js'
import { cutoffOf, type FilteredRoot, memberOf, memberRequest, membersAt, openUnion, type Refusal, refusalOf, refusalResponse, type Union, UnionOverlap, unionFilterFields, unionFor } from '../_lib/union.js'
import { filterThreshold } from '../_lib/filter.js'
import type { UnionView } from '../_lib/stores.js'

type DiffCtx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export const onRequestGet = async (ctx0: DiffCtx): Promise<Response> => {
  // A union view (`STORES_JSON` `union`, `_lib/union.ts`): each member diffs its own scans; a deployment without
  // one never leaves `diffOf`.
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  return u ? unionDiff(ctx0, u) : diffOf(ctx0)
}

/** A union member's diff (`unionDiff`): the identity its gate already resolved, the union root's common byte
 *  threshold, and whether its two scans may be one (a member that didn't rescan between the cutoffs). */
interface MemberDiff {
  id: Identity | null
  threshold?: number
  same?: boolean
}

async function diffOf(ctx0: DiffCtx, member?: MemberDiff): Promise<Response> {
  const st = serverTiming()
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = await withPathStore(ctx1)
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) {
    return new Response('diff API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const from = url.searchParams.get('from') ?? ''
  const to = url.searchParams.get('to') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  const w = Math.ceil((Number(url.searchParams.get('w')) || 1280) / QUANT) * QUANT
  const h = Math.ceil((Number(url.searchParams.get('h')) || 800) / QUANT) * QUANT
  const minArea = Number(url.searchParams.get('minArea')) || MIN_AREA_DEFAULT
  const atten = Number(url.searchParams.get('atten')) || ATTEN_DEFAULT
  const top = Math.min(5000, Number(url.searchParams.get('top')) || 500)
  const summary = url.searchParams.get('summary') === '1'
  const depth = Number(url.searchParams.get('depth')) || undefined
  if (!isScanId(from) || !isScanId(to)) return new Response('bad from/to', { status: 400 })
  if (from > to || (from === to && !member?.same)) return new Response('from must precede to', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })

  const lensRaw = url.searchParams.get('lens')
  let lens: Lens | undefined
  if (lensRaw) {
    if (ctx.env.STORE_KEY) return new Response(LENS_PRIMARY_ONLY, { status: 400 })
    const m = /^user:(.+)$/.exec(lensRaw)
    if (!m) return new Response('bad lens (want user:<id>)', { status: 400 })
    lens = { key: m[1] }
  }
  const rawOwner = url.searchParams.get('o')
  const owner = parseOwner(rawOwner)
  const classes = parseClasses(url.searchParams.get('cl'))
  const qRaw = url.searchParams.get('q') ?? ''
  // `q=` in `qs=`'s syntax: a parse error (or an unknown syntax) is a 400
  // whose text the filter box shows under itself.
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, ctx.env.QUERY_SYNTAX, indexedOnly(ctx.env))
  } catch (e) {
    // Indexed-only: a form it refuses is refused as such (`a b` is "one term", not "too short").
    const r = e instanceof QueryError && indexedOnly(ctx.env) ? rejectQuery(url.searchParams.get('q'), pathTerms(ctx.env)) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  let query = qp.query
  // An indexed-only deployment: what the static index answers, no user lens or class scope (`_lib/indexedOnly.ts`), before auth or any read.
  const refused = query && indexedOnly(ctx.env) ? rejectQuery(qRaw, pathTerms(ctx.env)) ?? rejectScope(!!(lens || classes)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })

  // The gate, beside what the answer needs whoever asks (`shared.ahead`: started now for a request that may pass it,
  // used only once it has): both scans' checks, the cache key's generations and, unscoped, the cache lookup.
  const gate = member ? Promise.resolve(member.id) : st.time('auth', requireViewer(ctx as never))
  gate.catch(() => {})
  await loadScopes()
  const early = ahead(ctx.env, !!member || await presentsCredentials(ctx as never))
  try {
    return await diffGated(ctx, st, early, gate, { from, to, path, w, h, minArea, atten, top, summary, depth, lens, rawOwner, owner, classes, qRaw, qp, query, member })
  } finally {
    early.done()
  }
}

/** `diffOf` past its parameters: the gate, the scans' checks, the cache lookup and the build. */
async function diffGated(
  ctx: DiffCtx,
  st: ReturnType<typeof serverTiming>,
  early: ReturnType<typeof ahead>,
  gate: Promise<Identity | null | Response>,
  o: { from: string; to: string; path: string; w: number; h: number; minArea: number; atten: number; top: number; summary: boolean; depth?: number; lens?: Lens; rawOwner: string | null; owner: ReturnType<typeof parseOwner>; classes: ReturnType<typeof parseClasses>; qRaw: string; qp: ReturnType<typeof queryParam>; query: ReturnType<typeof queryParam>['query']; member?: MemberDiff },
): Promise<Response> {
  const { from, to, path, w, h, minArea, atten, top, summary, depth, rawOwner, owner, classes, qRaw, qp, member } = o
  let { lens, query } = o
  const url = new URL(ctx.request.url)
  const scans = early.later(() => Promise.all(['from', 'to'].map(key => scanArg(ctx.env, url.searchParams, key))))
  const gens = early.later(() => pathGens(ctx.env, [from, to]))
  const descentOf = (lensOn: boolean) => url.searchParams.get('dm') === 'delta' && intervalsOn(ctx.env) && !lensOn && !owner && !query && !classes && !summary && atten >= 1
  const keyOf = (head: number, g: string, lensTag: string, descent: boolean) => cacheKeyFor('diff', cacheEnvTag(ctx.env, ctx.request),
    `${from}/${to}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&n=${top}&l=${lensTag}` +
      `&o=${rawOwner ?? ''}&cl=${classKey(classes)}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&s=${summary ? 1 : 0}&D=${depth ?? ''}&g=${g}&st=${staticTag(ctx.env, query)}${query ? `&fv=${FILTER_VIEW_V}` : ''}` +
      // A scoped read's answer version, where the diff is one (absent otherwise, so no other key moves).
      scopedTag(ctx.env, { lens, owner, classes, query }) +
      // A union root's member diff: the union's threshold (absent otherwise, so no other key moves).
      (member?.threshold !== undefined ? `&T=${member.threshold}` : '') + (descent && ctx.env.DIFF_DESCENT === '1' ? `&dm=d${DESCENT_V}` : ''),
    storeKey(ctx.env),
  )
  // Unscoped and unfiltered (the map's diffs): the key is the gate's to none of (no lens, ledger head or name rule), so
  // its lookup starts beside the gate too.
  const plain = !lens && !owner && !query
  const early1 = plain ? early.later(async () => {
    const g = await gens()
    const key = keyOf(0, g, lensParam(undefined), descentOf(false))
    return { key, hit: await st.time('match', cacheMatch(ctx.env, key)) }
  }) : null

  const gated = await gate
  if (gated instanceof Response) return gated
  early.commit()
  // A path-restricted link (specs/path-scopes.md): a hidden path is absent,
  // and every body (cached or fresh) is filtered on the way out.
  const restriction = restrictionFor(gated, ctx.env)
  if (restriction && isHidden(path, restriction)) return hiddenNotFound()
  // A name filter's totals sum every match, hidden ones too (`/api/subtree`'s refusal).
  if (restriction && qp.query) return restrictedForbidden()
  const out = (res: Response | Promise<Response>) =>
    restriction ? Promise.resolve(res).then(r => filterJson(r, (b: Parameters<typeof restrictDiff>[0]) => restrictDiff(b, restriction))) : res
  // Both ends must be indexed scans: a miss is a 404, never another scan.
  const miss = (await scans()).find(r => r instanceof Response)
  if (miss) return miss

  // One guard over the D1 pre-step, the cache match and the build (see
  // subtree.ts): a D1 stall becomes a retryable 503, not a raw 500 page.
  try {
    // `user:me` → the caller's id: the shared cache key and the body carry the id.
    const resolved = await resolveLens(ctx.env, gated, lens)
    if (resolved === null) return new Response(ME_UNRESOLVED, { status: 400 })
    lens = resolved
    const lensTag = lensParam(lens)
    // The static index's hex-run rule: substrings match where they `occur` under it, static or not (`hexRuns.ts`).
    const hx = await hexQuery(ctx.env, query)
    query = hx.query
    const [head, g] = await st.time('pre', Promise.all([(lens || owner) && await hasLedger(ctx.env) ? ledgerHead(ctx.env) : Promise.resolve(0), gens()]))
    // Δ mode from the transitions (specs/diff-from-changes.md): asked for explicitly (`dm=delta`: a client that doesn't
    // say draws max(old, new) areas from these rows too), unscoped, on a deployment whose store has them.
    // `atten ≥ 1`: a parent's floor is then at most its children's, so a changed child's parent (as big) is read too.
    const deltaOk = descentOf(!!lens)
    const descent = deltaOk && ctx.env.DIFF_DESCENT === '1'
    const cacheKey = keyOf(head, g, lensTag, deltaOk)
    // …and both scans covered by the static index for this literal (else `scan-not-indexed`).
    if (query && indexedOnly(ctx.env)) {
      const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [from, to]))
      if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    // The worker's answer: the diff's JSON and what to keep of it — `phase2Ms` (a background full run's) over
    // the viewer-facing default.
    const render = async (o: { phase2Ms?: number; searchLimits?: SearchLimits; walkMs?: number; trace?: typeof st.trace } = {}) => {
      const diff = await ivRetry(() => buildDiff(ctx.env, { from, to, path, w, h, minArea, atten, top, lens, owner, query, classes, summary, depth, trace: o.trace, ...(o.phase2Ms ? { phase2Ms: o.phase2Ms } : {}), ...(o.searchLimits ? { searchLimits: o.searchLimits } : {}), ...(o.walkMs ? { walkMs: o.walkMs } : {}), ...(member?.threshold !== undefined ? { threshold: member.threshold } : {}), ...(descent ? { descent } : {}) }))
      // Cut by a time budget (phase 2's or the search's): kept briefly, upgraded at the edge, re-asked (`budgetCut`).
      const keep = keepFor(diff)
      const body = JSON.stringify({
        prev: from,
        curr: to,
        path,
        ...(lens ? { lens: lensTag } : {}),
        ...(owner ? { owner } : {}),
        ...(query ? { q: qRaw, ...hexNote(hx.hexRuns, query.ast) } : {}),
        ...diff,
        threshold: Math.round(diff.threshold),
        ...(keep !== true ? { budgetCut: true } : {}),
      })
      return { body, keep }
    }
    // A partial answer schedules the full one in the background (`upgradePartial`, as subtree.ts).
    const upgrade = (res: Response): Response => {
      res.headers.set('x-cache-upgrade', upgradePartial(ctx.env, cacheKey, () => render({ phase2Ms: UPGRADE_PHASE2_MS, searchLimits: UPGRADE_SEARCH_LIMITS, walkMs: UPGRADE_WALK_MS }), ctx.waitUntil?.bind(ctx)))
      return res
    }
    const looked = early1 && await early1()
    const hit = looked && looked.key.url === cacheKey.url ? looked.hit : await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return out(isPartial(hit) ? upgrade(hit) : hit)

    st.mark('fixed')
    const { body, keep } = await render({ trace: st.trace })
    // A phase 2 cut short by its time budget: kept briefly (`keepFor`, as subtree.ts).
    const res = await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keep)
    return out(keep === true ? res : upgrade(res))
  } catch (e) {
    const refused = ivRefusal(e)
    if (refused) return refused
    if (e instanceof NotFound) return new Response('path not found in either scan', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof LensUnavailable) return new Response('lens index not available for a scan', { status: 409 })
    if (e instanceof DiffUnavailable) return new Response(e.message, { status: 409 })
    const msg = String((e as Error).message ?? e)
    if (msg.startsWith('query too wide')) return new Response(msg, { status: 413 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}

/** A member's change between a union diff's two cutoffs: its scans differ (`changed`), it didn't rescan
 *  (`unchanged`: zero change), or it has no scan by one of them (`unknown`: never added or removed). */
export type MemberChange = 'changed' | 'unchanged' | 'unknown'

/**
 * `/api/diff` on a union view. Both cutoffs (`from`, `to`) resolve independently, then each member diffs its own
 * scans at them, through its own read path. The root: the changed members' diffs under one byte threshold from
 * the union's totals, merged — their depth-1 rows tagged with their store (`src`), their `(other)`s summed —
 * and every comparable member's totals summed; an unchanged member (no rescan between the cutoffs) adds its
 * root total to both sides and no rows: zero change. A member unknown at either cutoff is left out — of the rows and of the totals — and named
 * (`union.sources[].change`): new coverage is not growth, lost coverage not deletion. Below the root: the path's
 * member (at `to`, else at `from`) answers — an unchanged one its one scan against itself (every total equal, no
 * rows); an unknown one is a 409.
 */
/** A union's own time before its members' reads (`union`), then its parts: the members' contexts (`union-ctxs`),
 *  gates (`union-gate`) and observations (`union-obs`; these two concurrently), the cutoffs (`union-cutoff`), and the
 *  drilled member's lookup (`union-member`) or the root's totals (`union-roots`). */
type UnionMarks = { un?: Union; cutoff?: number; member?: number; roots?: number }
const unionTiming = (pre: number, m: UnionMarks): string => [
  ['union', pre], ...(m.un ? [['union-ctxs', m.un.timing.ctxs], ['union-gate', m.un.timing.gate], ['union-obs', m.un.timing.obs]] : []),
  ...(['cutoff', 'member', 'roots'] as const).flatMap(k => m[k] === undefined ? [] : [[`union-${k}`, m[k]!]]),
].map(([k, ms]) => `${k};dur=${Math.round(ms as number)}`).join(', ')

/** A union response with its own timing (`unionTiming`) first in its Server-Timing, before the member's entries. */
function withUnionTiming(r: Response, own: string): Response {
  const out = new Response(r.body, r)
  const st = r.headers.get('server-timing')
  out.headers.set('server-timing', `${own}${st ? `, ${st}` : ''}`)
  return out
}

async function unionDiff(ctx0: DiffCtx, view: UnionView): Promise<Response> {
  const t0 = performance.now()
  const url = new URL(ctx0.request.url)
  const sp = url.searchParams
  const path = (sp.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  try {
    const un = await openUnion(ctx0, view, true)
    if (un instanceof Response) return un
    const marks: UnionMarks = { un }
    const tc = performance.now()
    const cf = await cutoffOf(un, sp, 'from')
    if (cf instanceof Response) return cf
    const ct = await cutoffOf(un, sp, 'to')
    if (ct instanceof Response) return ct
    marks.cutoff = performance.now() - tc
    if (cf.at >= ct.at) return new Response('from must precede to', { status: 400 })
    const [rf, rt] = [membersAt(un, cf), membersAt(un, ct)]
    const pairs = view.members.map((store, i) => {
      const from = rf.members[i].date, to = rt.members[i].date
      const change: MemberChange = from && to ? (from === to ? 'unchanged' : 'changed') : 'unknown'
      return { store, env: rt.members[i].ctx.env, from, to, change }
    })
    const sources = pairs.map(({ store, from, to, change }) => ({ store, from, to, change }))
    const ask = (p: typeof pairs[number], m: MemberDiff) =>
      diffOf({ ...ctx0, request: memberRequest(ctx0.request, p.store, { from: p.from!, to: p.to! }) }, { ...m, same: p.change === 'unchanged' })
    if (path !== '') {
      const tm = performance.now()
      const m = await memberOf(rt, path) ?? await memberOf(rf, path)
      marks.member = performance.now() - tm
      if (!m) return new Response('path not found in either scan', { status: 404 })
      const p = pairs[view.members.indexOf(m.store)]
      if (p.change === 'unknown') return json({ error: `store '${p.store}' has no scan by ${p.from ? ct.date : cf.date}: its change is unknown`, union: { view: view.key, sources } }, 409)
      const pre = performance.now() - t0
      return withUnionTiming(await ask(p, { id: un.id }), unionTiming(pre, marks))
    }
    const scoped = (['lens', 'o', 'cl'] as const).filter(k => sp.get(k))
    if (scoped.length) return new Response(`a union view's root is unscoped (${scoped.join(', ')}: drill into one store)`, { status: 400 })
    const w = Math.ceil((Number(sp.get('w')) || 1280) / QUANT) * QUANT
    const h = Math.ceil((Number(sp.get('h')) || 800) / QUANT) * QUANT
    const minArea = Number(sp.get('minArea')) || MIN_AREA_DEFAULT
    const comparable = pairs.filter(p => p.change !== 'unknown')
    const q = sp.get('q') ?? ''
    if (q.trim()) {
      const pre = performance.now() - t0
      const answer = await unionFilteredDiff(view, comparable, (p, threshold) => ask(p, { id: un.id, ...(threshold === undefined ? {} : { threshold }) }), { w, h, minArea })
      if (answer instanceof Response) return answer
      const { reads, refused, ...body } = answer
      return new Response(JSON.stringify({ prev: cf.date, curr: ct.date, path, q, ...body, union: { view: view.key, sources: sources.map(s => refusedOf(s, refused)) } }), { headers: {
        'content-type': 'application/json; charset=utf-8',
        'server-timing': `${unionTiming(pre, marks)}, members;dur=${Math.round(performance.now() - t0 - pre)}`,
        'cache-control': reads.map(r => r.headers.get('cache-control')).find(c => c === 'no-store') ?? reads[0]?.headers.get('cache-control') ?? 'no-store',
      } })
    }
    const tr = performance.now()
    const roots = await Promise.all(comparable.map(p => Promise.all([p.from!, p.to!].map(date => ivRetry(() => readRootAgg(p.env, { date, path: '' }))))))
    marks.roots = performance.now() - tr
    const side = (k: 0 | 1) => roots.reduce((n, r) => n + (r[k]?.b ?? 0), 0)
    const threshold = (Math.max(side(0), side(1)) * minArea) / (w * h)
    // A changed member's diff is read; an unchanged one's is its root total on both sides (no rows: nothing moved).
    const changed = comparable.filter(p => p.change === 'changed')
    const pre = performance.now() - t0
    const reads = await Promise.all(changed.map(p => ask(p, { id: un.id, threshold })))
    const bad = reads.find(r => !r.ok)
    if (bad) return bad
    type Body = { rows: DiffRow[]; total_a: number; total_b: number; objects_a: number; objects_b: number; expansions: number; truncated: boolean; tier: string; lookups: number; lookups_capped: boolean; budgetCut?: true }
    const bodies = await Promise.all(reads.map(r => r.json() as Promise<Body>))
    const still = comparable.flatMap((p, i) => p.change === 'unchanged' ? [roots[i][0] ?? { b: 0, o: 0 }] : [])
    const sum = (k: 'total_a' | 'total_b' | 'objects_a' | 'objects_b' | 'expansions' | 'lookups') =>
      bodies.reduce((n, b) => n + b[k], 0) + (k === 'expansions' || k === 'lookups' ? 0 : still.reduce((n, r) => n + (k.startsWith('total') ? r.b : r.o), 0))
    return new Response(JSON.stringify({
      prev: cf.date, curr: ct.date, path,
      rows: unionDiffRows(view, bodies.map((b, i) => ({ store: changed[i].store, rows: b.rows, total: { a: b.total_a, b: b.total_b, oa: b.objects_a, ob: b.objects_b } }))),
      total_a: sum('total_a'), total_b: sum('total_b'), objects_a: sum('objects_a'), objects_b: sum('objects_b'),
      expansions: sum('expansions'),
      truncated: bodies.some(b => b.truncated),
      threshold: Math.round(threshold),
      tier: bodies[0]?.tier ?? 'none',
      lookups: sum('lookups'),
      lookups_capped: bodies.some(b => b.lookups_capped),
      ...(bodies.some(b => b.budgetCut) ? { budgetCut: true } : {}),
      union: { view: view.key, sources },
    }), { headers: {
      'content-type': 'application/json; charset=utf-8',
      'server-timing': `${unionTiming(pre, marks)}, members;dur=${Math.round(performance.now() - t0 - pre)}`,
      'cache-control': reads.map(r => r.headers.get('cache-control')).find(c => c === 'no-store') ?? reads[0]?.headers.get('cache-control') ?? 'no-store',
    } })
  } catch (e) {
    const refused = ivRefusal(e)
    if (refused) return refused
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}

type MemberDiffBody = { rows: DiffRow[]; total_a: number; total_b: number; objects_a: number; objects_b: number; expansions: number; truncated: boolean; tier: string; lookups: number; lookups_capped: boolean; lookups_late?: true; searchLate?: true; budgetCut?: true } & Omit<FilteredRoot, 'tree'>
type Comparable = { store: string; change: MemberChange }

/** A union source with its member's refusal of the filter beside it (`filteredVector`'s, for a diff's sources). */
const refusedOf = <S extends { store: string }>(s: S, refused: readonly Refusal[]) => {
  const x = refused.find(f => f.store === s.store)
  return x ? { ...s, refused: { code: x.code, error: x.error } } : s
}

/**
 * A union root's filtered diff (`q=`): every comparable member's filtered diff of its own scans (an unchanged one
 * its scan against itself: its matches' totals on both sides, no rows), the changed members' rows drawn under one
 * threshold from the members' matched totals (`filterThreshold`, re-asked when more than one member drew bytes),
 * merged as the plain root diff merges, with the filter fields by `unionFilterFields`. A member that refused the
 * filter for its mount contributes nothing and is named (partial); every member refusing is that refusal.
 */
async function unionFilteredDiff<P extends Comparable>(
  view: UnionView,
  comparable: readonly P[],
  ask: (p: P, threshold?: number) => Promise<Response>,
  o: { w: number; h: number; minArea: number },
): Promise<Response | Record<string, unknown> & { reads: Response[]; refused: Refusal[] }> {
  const reads = await Promise.all(comparable.map(p => ask(p)))
  const refused: Refusal[] = []
  const bodies = new Map<number, MemberDiffBody>()
  for (const [i, r] of reads.entries()) {
    if (r.ok) { bodies.set(i, await r.json() as MemberDiffBody); continue }
    const x = await refusalOf(comparable[i].store, r)
    if (!x) return r
    refused.push(x)
  }
  if (!bodies.size && refused.length) return refusalResponse(refused[0])
  const all = [...bodies.values()]
  const threshold = filterThreshold(Math.max(all.reduce((n, b) => n + b.total_a, 0), all.reduce((n, b) => n + b.total_b, 0)), o.w, o.h, o.minArea)
  const drawn = [...bodies.keys()].filter(i => comparable[i].change === 'changed' && (bodies.get(i)!.total_a > 0 || bodies.get(i)!.total_b > 0))
  if (drawn.length > 1) {
    const again = await Promise.all(drawn.map(i => ask(comparable[i], threshold)))
    const bad = again.find(r => !r.ok)
    if (bad) return bad
    for (const [k, i] of drawn.entries()) { reads[i] = again[k]; bodies.set(i, await again[k].json() as MemberDiffBody) }
  }
  const answered = [...bodies.keys()].sort((a, b) => a - b)
  const members = answered.map(i => ({ store: comparable[i].store, change: comparable[i].change, body: bodies.get(i)! }))
  const bs = members.map(m => m.body)
  const sum = (k: 'total_a' | 'total_b' | 'objects_a' | 'objects_b' | 'expansions' | 'lookups') => bs.reduce((n, b) => n + b[k], 0)
  return {
    rows: unionDiffRows(view, members.filter(m => m.change === 'changed').map(m => ({ store: m.store, rows: m.body.rows }))),
    total_a: sum('total_a'), total_b: sum('total_b'), objects_a: sum('objects_a'), objects_b: sum('objects_b'),
    expansions: sum('expansions'),
    truncated: bs.some(b => b.truncated),
    threshold: Math.round(threshold),
    tier: bs[0]?.tier ?? 'none',
    lookups: sum('lookups'),
    lookups_capped: bs.some(b => b.lookups_capped),
    ...(bs.some(b => b.lookups_late) ? { lookups_late: true } : {}),
    ...(bs.some(b => b.searchLate) ? { searchLate: true } : {}),
    ...(bs.some(b => b.budgetCut) ? { budgetCut: true } : {}),
    ...unionFilterFields(members, refused),
    reads: answered.map(i => reads[i]),
    refused,
  }
}

/** A union root diff's rows from its members' (each a root diff): every row as its member drew it, a depth-1
 *  row tagged with its store (`src`), and one `(other)`. With a member's totals (`total`), its share of `(other)` is
 *  the invariant — its totals less its drawn depth-1 rows, per side, clamped — so a member that drew nothing at the
 *  union's threshold (its root not expanded, no `(other)` of its own) still adds up; without, its own depth-1
 *  `(other)` (a filtered diff's fold). A depth-1 name two members draw is a misconfigured union (`UnionOverlap`). */
export function unionDiffRows(view: UnionView, members: readonly { store: string; rows: readonly DiffRow[]; total?: { a: number; b: number; oa: number; ob: number } }[]): (DiffRow & { src?: string })[] {
  const out: (DiffRow & { src?: string })[] = []
  const other = { a: 0, b: 0, oa: 0, ob: 0 }
  let any = false
  const seen = new Map<string, string>()
  for (const m of members) {
    const drawn = { a: 0, b: 0, oa: 0, ob: 0 }
    for (const r of m.rows) {
      if (r.d !== 1) { out.push(r); continue }
      if (r.p === '(other)') {
        any = true
        if (!m.total) { other.a += r.a; other.b += r.b; other.oa += r.oa; other.ob += r.ob }
        continue
      }
      const prev = seen.get(r.p)
      if (prev) throw new UnionOverlap(`union view '${view.key}': root '${r.p}' is in stores ${prev}, ${m.store}`)
      seen.set(r.p, m.store)
      out.push({ ...r, src: m.store })
      drawn.a += r.a; drawn.b += r.b; drawn.oa += r.oa; drawn.ob += r.ob
    }
    if (m.total) {
      const t = m.total
      other.a += Math.max(0, t.a - drawn.a); other.b += Math.max(0, t.b - drawn.b)
      other.oa += Math.max(0, t.oa - drawn.oa); other.ob += Math.max(0, t.ob - drawn.ob)
    }
  }
  if (any || other.a !== other.b || other.oa !== other.ob) {
    out.push({ p: '(other)', d: 1, k: 'dir', a: other.a, b: other.b, oa: other.oa, ob: other.ob, s: other.a === other.b && other.oa === other.ob ? 'unchanged' : 'changed' } as DiffRow)
  }
  return out
}
