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
import { filterJson, hiddenNotFound, isHidden, restrictDiff, restrictionFor } from '../_lib/restrict.js'
import { type Env, type Identity, json, requireViewer } from '../_lib/auth.js'
import { ivRetry, pathGens, withPathStore, storeReady, type Lens } from '../_lib/index.js'
import { classKey, parseClasses, parseOwner, queryParam, QueryError } from '../_lib/scope.js'
import { ATTEN_DEFAULT, buildDiff, type DiffRow, FILTER_VIEW_V, LensUnavailable, MIN_AREA_DEFAULT, NotFound, QUANT, readRootAgg } from '../_lib/view.js'
import { hexNote, hexQuery, indexedGate, staticTag } from '../_lib/staticFilter.js'
import { FilterRejected, indexedOnly, rejectBody, rejectQuery, rejectScope } from '../_lib/indexedOnly.js'
import { cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, isPartial, keepFor, serverTiming, UPGRADE_PHASE2_MS, upgradePartial } from '../_lib/edgeCache.js'
import { LENS_PRIMARY_ONLY, storeKey, withStore } from '../_lib/stores.js'
import { lensParam, ME_UNRESOLVED, resolveLens } from '../_lib/me.js'
import { isScanId } from '../../src/scanSlug.js'
import { scanArg } from '../_lib/scanArg.js'
import { traceJoins } from '../_lib/shared.js'
import { cutoffOf, memberOf, memberRequest, membersAt, openUnion, UnionOverlap, unionFor } from '../_lib/union.js'
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
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx1 = withStore(ctx0)
  if (ctx1 instanceof Response) return ctx1
  const ctx = withPathStore(ctx1)
  const st = serverTiming()
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
    qp = queryParam(url.searchParams, ctx.env.QUERY_SYNTAX)
  } catch (e) {
    // Indexed-only: a form it refuses is refused as such (`a b` is "one term", not "too short").
    const r = e instanceof QueryError && indexedOnly(ctx.env) ? rejectQuery(url.searchParams.get('q'), url.searchParams.get('qs'), ctx.env.QUERY_SYNTAX) : null
    if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  let query = qp.query
  // An indexed-only deployment: one literal, unscoped (`_lib/indexedOnly.ts`), before auth or any read.
  const refused = query && indexedOnly(ctx.env) ? rejectQuery(qRaw, url.searchParams.get('qs'), ctx.env.QUERY_SYNTAX) ?? rejectScope(!!(lens || owner || classes)) : null
  if (refused) return new Response(rejectBody(refused), { status: 400, headers: { 'content-type': 'application/json' } })

  const gated = member ? member.id : await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  // A path-restricted link (specs/path-scopes.md): a hidden path is absent,
  // and every body (cached or fresh) is filtered on the way out.
  const restriction = restrictionFor(gated, ctx.env)
  if (restriction && isHidden(path, restriction)) return hiddenNotFound()
  const out = (res: Response | Promise<Response>) =>
    restriction ? Promise.resolve(res).then(r => filterJson(r, (b: Parameters<typeof restrictDiff>[0]) => restrictDiff(b, restriction))) : res
  // Both ends must be indexed scans: a miss is a 404, never another scan.
  const miss = (await Promise.all(['from', 'to'].map(key => scanArg(ctx.env, url.searchParams, key)))).find(r => r instanceof Response)
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
    const [head, g] = await st.time('pre', Promise.all([Promise.resolve(0), pathGens(ctx.env, [from, to])]))
    const cacheKey = cacheKeyFor('diff', cacheEnvTag(ctx.env, ctx.request),
      `${from}/${to}/${encodeURIComponent(path)}?w=${w}&h=${h}&a=${minArea}&t=${atten}&n=${top}&l=${lensTag}` +
        `&o=${rawOwner ?? ''}&cl=${classKey(classes)}&qs=${query ? qp.syntax : ''}&q=${encodeURIComponent(query ? qRaw : '')}&head=${head}&s=${summary ? 1 : 0}&D=${depth ?? ''}&g=${g}&st=${staticTag(ctx.env, query)}${query ? `&fv=${FILTER_VIEW_V}` : ''}` +
        // A union root's member diff: the union's threshold (absent otherwise, so no other key moves).
        (member?.threshold !== undefined ? `&T=${member.threshold}` : ''),
      storeKey(ctx.env),
    )
    // …and both scans covered by the static index for this literal (else `scan-not-indexed`).
    if (query && indexedOnly(ctx.env)) {
      const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [from, to]))
      if (r) return new Response(rejectBody(r), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    // The worker's answer: the diff's JSON and what to keep of it — `phase2Ms` (a background full run's) over
    // the viewer-facing default.
    const render = async (o: { phase2Ms?: number; trace?: typeof st.trace } = {}) => {
      const diff = await ivRetry(() => buildDiff(ctx.env, { from, to, path, w, h, minArea, atten, top, lens, owner, query, classes, summary, depth, trace: o.trace, ...(o.phase2Ms ? { phase2Ms: o.phase2Ms } : {}), ...(member?.threshold !== undefined ? { threshold: member.threshold } : {}) }))
      const body = JSON.stringify({
        prev: from,
        curr: to,
        path,
        ...(lens ? { lens: lensTag } : {}),
        ...(owner ? { owner } : {}),
        ...(query ? { q: qRaw, ...hexNote(hx.hexRuns, query.ast) } : {}),
        ...diff,
        threshold: Math.round(diff.threshold),
        ...(diff.interiors?.late ? { budgetCut: true } : {}),
      })
      return { body, keep: keepFor(diff.interiors) }
    }
    // A partial answer schedules the full one in the background (`upgradePartial`, as subtree.ts).
    const upgrade = (res: Response): Response => {
      res.headers.set('x-cache-upgrade', upgradePartial(ctx.env, cacheKey, () => render({ phase2Ms: UPGRADE_PHASE2_MS }), ctx.waitUntil?.bind(ctx)))
      return res
    }
    const hit = await st.time('match', cacheMatch(ctx.env, cacheKey))
    if (hit) return out(isPartial(hit) ? upgrade(hit) : hit)

    const { body, keep } = await render({ trace: st.trace })
    // A phase 2 cut short by its time budget: kept briefly (`keepFor`, as subtree.ts).
    const res = await cacheStore(ctx.env, cacheKey, body, { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), keep)
    return out(keep === true ? res : upgrade(res))
  } catch (e) {
    if (e instanceof NotFound) return new Response('path not found in either scan', { status: 404 })
    if (e instanceof FilterRejected) return new Response(rejectBody(e.reject), { status: 400, headers: { 'content-type': 'application/json' } })
    if (e instanceof LensUnavailable) return new Response('lens index not available for a scan', { status: 409 })
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
async function unionDiff(ctx0: DiffCtx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const sp = url.searchParams
  const path = (sp.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
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
    const ask = (p: typeof pairs[number], m: MemberDiff) =>
      diffOf({ ...ctx0, request: memberRequest(ctx0.request, p.store, { from: p.from!, to: p.to! }) }, { ...m, same: p.change === 'unchanged' })
    if (path !== '') {
      const m = await memberOf(rt, path) ?? await memberOf(rf, path)
      if (!m) return new Response('path not found in either scan', { status: 404 })
      const p = pairs[view.members.indexOf(m.store)]
      if (p.change === 'unknown') return json({ error: `store '${p.store}' has no scan by ${p.from ? ct.date : cf.date}: its change is unknown`, union: { view: view.key, sources } }, 409)
      return ask(p, { id: un.id })
    }
    const scoped = (['lens', 'o', 'cl', 'q'] as const).filter(k => sp.get(k))
    if (scoped.length) return new Response(`a union view's root is unscoped (${scoped.join(', ')}: drill into one store)`, { status: 400 })
    const w = Math.ceil((Number(sp.get('w')) || 1280) / QUANT) * QUANT
    const h = Math.ceil((Number(sp.get('h')) || 800) / QUANT) * QUANT
    const minArea = Number(sp.get('minArea')) || MIN_AREA_DEFAULT
    const comparable = pairs.filter(p => p.change !== 'unknown')
    const roots = await Promise.all(comparable.map(p => Promise.all([p.from!, p.to!].map(date => ivRetry(() => readRootAgg(p.env, { date, path: '' }))))))
    const side = (k: 0 | 1) => roots.reduce((n, r) => n + (r[k]?.b ?? 0), 0)
    const threshold = (Math.max(side(0), side(1)) * minArea) / (w * h)
    // A changed member's diff is read; an unchanged one's is its root total on both sides (no rows: nothing moved).
    const changed = comparable.filter(p => p.change === 'changed')
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
      rows: unionDiffRows(view, bodies.map((b, i) => ({ store: changed[i].store, rows: b.rows }))),
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
      'cache-control': reads.map(r => r.headers.get('cache-control')).find(c => c === 'no-store') ?? reads[0]?.headers.get('cache-control') ?? 'no-store',
    } })
  } catch (e) {
    const msg = String((e as Error).message ?? e)
    if (e instanceof UnionOverlap) return new Response(msg, { status: 500 })
    if (/D1_ERROR|internal error/i.test(msg)) return new Response(`index backend unavailable, retry: ${msg}`, { status: 503, headers: { 'retry-after': '5' } })
    return new Response(`diff failed: ${msg}`, { status: 500 })
  }
}

/** A union root diff's rows from its members' (each a root diff): every row as its member drew it, a depth-1
 *  row tagged with its store (`src`), the members' depth-1 `(other)` folds summed into one. A depth-1 name two
 *  members draw is a misconfigured union (`UnionOverlap`). */
export function unionDiffRows(view: UnionView, members: readonly { store: string; rows: readonly DiffRow[] }[]): (DiffRow & { src?: string })[] {
  const out: (DiffRow & { src?: string })[] = []
  let other = null as DiffRow | null
  const seen = new Map<string, string>()
  for (const m of members) {
    for (const r of m.rows) {
      if (r.d !== 1) { out.push(r); continue }
      if (r.p === '(other)') {
        other = other ? { ...other, a: other.a + r.a, b: other.b + r.b, oa: other.oa + r.oa, ob: other.ob + r.ob } : { ...r }
        continue
      }
      const prev = seen.get(r.p)
      if (prev) throw new UnionOverlap(`union view '${view.key}': root '${r.p}' is in stores ${prev}, ${m.store}`)
      seen.set(r.p, m.store)
      out.push({ ...r, src: m.store })
    }
  }
  if (other) out.push({ ...other, s: other.a === other.b && other.oa === other.ob ? 'unchanged' : 'changed' })
  return out
}
