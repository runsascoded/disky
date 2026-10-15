/** Union views (specs/federated-scans.md, "First live cases" 1): one
 * deployment's stores read side by side at request time.
 *
 * A union view (`STORES_JSON` entry `{"union": [<store>…], "main"?: true}`,
 * `stores.ts`) names member stores of the same deployment. A request on it
 * resolves **one cutoff** (the scan the request names, in whichever member
 * indexes it), then each member's scan at that cutoff — the latest it
 * observed through it (`latestObserved`, the federated resolver's rule), so
 * members whose scan ids happen to align pick the same id and members that
 * scan on their own schedules carry their last scan forward. A member with
 * no scan by the cutoff is unknown (`date: null`), never its first later
 * scan.
 *
 * The union's root is its members' roots side by side; any other path
 * belongs to the one member whose roots hold its first segment, and is
 * answered by that member's own read path (`store=<member>`, `withStore`) at
 * its resolved scan — so its cache keys are the member's own, carrying that
 * scan. Members' roots must be disjoint (a shared root is a config error,
 * never a sum).
 *
 * Nothing here names a deployment or a store: the config drives it. A
 * deployment without a union view never reaches this module.
 */
import { decodeScan, isScanId, scanInstant } from '../../src/scanSlug.js'
import { compareScans, latestObserved, type Observation, parseCutoff } from '../../src/federatedScans.js'
import { type Env, json, type Identity, requireViewer } from './auth.js'
import { ivRetry, pathScans, storeReady, withPathStore } from './index.js'
import { indexedScan, noScan } from './scanArg.js'
import { scanTimes } from './scanTimes.js'
import { PRIMARY_STORE, StoreConfigError, type UnionView, unionViews, withStore } from './stores.js'
import { readRootRows, type ViewNode } from './view.js'

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

/** The union view a request reads, if any: `store=<view key>`, or no `store=`
 * on a deployment with a main view. `store=primary` (and any store key) is
 * that store alone. A malformed `STORES_JSON` is a 500. */
export function unionFor(request: Request, env: Env): UnionView | null | Response {
  if (!env.STORES_JSON) return null
  let views: Record<string, UnionView>
  try {
    views = unionViews(env)
  } catch (e) {
    if (e instanceof StoreConfigError) return json({ error: e.message }, 500)
    throw e
  }
  const raw = new URL(request.url).searchParams.get('store')
  if (raw === null || raw === '') return Object.values(views).find(v => v.main) ?? null
  return views[raw] ?? null
}

/** The params only a union view reads (its `at`-form cutoffs): never passed on to a member, whose own handler
 *  takes the scan id the union resolved for it. */
const UNION_ONLY = ['at', 'from_at', 'to_at']

/** `request` as member `store` asks it: `store=<member>` (none for the
 * primary), `set` params replaced, `drop` params (and the union's own
 * `at`-form cutoffs) removed. Headers (the
 * session cookie), method and signal carry over. */
export function memberRequest(request: Request, store: string, set: Record<string, string> = {}, drop: string[] = []): Request {
  const url = new URL(request.url)
  if (store === PRIMARY_STORE) url.searchParams.delete('store')
  else url.searchParams.set('store', store)
  for (const k of [...UNION_ONLY, ...drop]) url.searchParams.delete(k)
  for (const [k, v] of Object.entries(set)) url.searchParams.set(k, v)
  return new Request(url, request)
}

/** One member of a resolved union: its context (the store's env overlay, as
 * `withStore` gives it) and its scan at the cutoff (null = unknown). */
export interface Member {
  store: string
  ctx: Ctx
  date: string | null
}

/** A union resolved at one cutoff: the scan the request named (`date`, the
 * client's as-of), its instant (`at`), and every member's scan at it. */
export interface Resolved {
  view: UnionView
  date: string
  at: string
  members: Member[]
}

/** The `union` block a response carries: the per-store vector it was read at. */
export const vectorOf = (r: Resolved): { view: string; at: string; sources: { store: string; date: string | null }[] } =>
  ({ view: r.view.key, at: r.at, sources: r.members.map(m => ({ store: m.store, date: m.date })) })

/** Each member's context for `request` (`withStore` + `withPathStore`, as a
 * single-store handler builds its own). */
export async function memberCtxs(ctx: Ctx, view: UnionView): Promise<Ctx[] | Response> {
  const out: Ctx[] = []
  for (const store of view.members) {
    const c = withStore({ ...ctx, request: memberRequest(ctx.request, store) })
    if (c instanceof Response) return c
    out.push(await withPathStore(c))
  }
  return out
}

const iso = (ms: number): string => new Date(ms).toISOString()

/** A member's indexed scans as observations: each id observed through its
 * own instant (`scanInstant`: a timed id's minute, a date-only id's recorded
 * start, else its midnight). One D1 query for the ids, one for the starts. */
async function observations(env: Env): Promise<Observation[]> {
  const dates = (await pathScans(env, true)).results.map(r => r.date)
  const times = await scanTimes(env, dates)
  return dates.map(id => { const t = iso(scanInstant(id, times)); return { id, revision: '', observed_through: t, published_at: t } })
}

/** A union opened for a request: its members' contexts (gated) and indexed scans, and the caller's identity. */
export interface Union {
  view: UnionView
  ctxs: Ctx[]
  obs: Observation[][]
  id: Identity | null
}

/** A cutoff: the scan id that names it (the client's as-of) and its instant. */
export interface Cutoff { date: string; at: string }

/**
 * Open a union for a request: every member's context, gated first
 * (`requireViewer` on its own env: a union demands each member's scope, so
 * nothing is read for a viewer who may not see all of it), then its indexed
 * scans as observations.
 */
export async function openUnion(ctx: Ctx, view: UnionView, gate: boolean): Promise<Union | Response> {
  const ctxs = await memberCtxs(ctx, view)
  if (ctxs instanceof Response) return ctxs
  for (const c of ctxs) {
    if (!c.env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
    if (!storeReady(c.env)) return json({ error: 'index reader not configured (missing index store creds)' }, 503)
  }
  let id: Identity | null = null
  if (gate) {
    for (const c of ctxs) {
      const g = await requireViewer(c as never)
      if (g instanceof Response) return g
      id ??= g
    }
  }
  return { view, ctxs, obs: await Promise.all(ctxs.map(c => observations(c.env))), id }
}

/** The `at`-form param beside a scan-id param: `date` → `at`, `from` → `from_at`, `to` → `to_at`. */
export const atKey = (key: string): string => (key === 'date' ? 'at' : `${key}_at`)

/**
 * The cutoff a request names under `key`, three ways:
 * - `<key>=<id>`: an exact scan id some member indexes (else the single-store 404), at its instant;
 * - `<atKey>=<instant>` (`at=`, `from_at=`, `to_at=`; `parseCutoff`: ISO or compact UTC): that instant itself —
 *   each member then reads its newest scan at or before it, so a cutoff between two members' scans (one
 *   member's newer scan, another's older) is said exactly, which no one scan id can; `date` is the newest scan
 *   any member observed by then (none: a 404);
 * - for `date` absent, `d=<slug>`: the latest scan any member indexes under the slug.
 */
export async function cutoffOf(u: Union, sp: URLSearchParams, key = 'date'): Promise<Cutoff | Response> {
  const raw = sp.get(key)
  const atRaw = raw === null ? sp.get(atKey(key)) : null
  if (atRaw !== null) {
    const at = parseCutoff(atRaw)
    if (!at) return json({ error: `${atKey(key)}=<UTC instant> (YYYY-MM-DDTHH:MM[:SS]Z or YYYYMMDDTHHMM[SS]Z) required` }, 400)
    const best = u.obs.flatMap(o => (latestObserved(o, at) ? [latestObserved(o, at)!] : [])).sort(compareScans).at(-1)
    return best ? { date: best.id, at } : json({ error: `no scan at or before ${atKey(key)}=${atRaw}` }, 404)
  }
  if (raw !== null || key !== 'date' || !sp.has('d')) {
    if (!raw || !isScanId(raw)) return json({ error: `${key}=YYYY-MM-DD[THHMM] required` }, 400)
    const hit = u.obs.map(o => o.find(s => s.id === raw)).find(Boolean)
    return hit ? { date: raw, at: hit.observed_through } : noScan(key, raw)
  }
  const slug = sp.get('d') ?? ''
  const prefix = decodeScan(slug)
  const found = prefix ? await Promise.all(u.ctxs.map(c => indexedScan(c.env, prefix, false))) : []
  const cands = found.map((f, i) => (f ? u.obs[i].find(s => s.id === f) ?? null : null)).filter((s): s is Observation => !!s)
  const best = cands.sort(compareScans).at(-1)
  return best ? { date: best.id, at: best.observed_through } : noScan('d', slug)
}

/** Whether a request names a cutoff at all (`date=`, `at=` or `d=`). */
export const namesCutoff = (sp: URLSearchParams): boolean => ['date', 'at', 'd'].some(k => sp.has(k))

/** The newest scan any member indexes: the "latest" cutoff (null = no member has one). */
export function latestCutoff(u: Union): Cutoff | null {
  const best = u.obs.flat().sort(compareScans).at(-1)
  return best ? { date: best.id, at: best.observed_through } : null
}

/** Every member's scan at a cutoff: its latest observed through it (null = unknown, never a later one). */
export function membersAt(u: Union, c: Cutoff): Resolved {
  return { view: u.view, ...c, members: u.view.members.map((store, i) => ({ store, ctx: u.ctxs[i], date: latestObserved(u.obs[i], c.at)?.id ?? null })) }
}

/** Open, cut off at the request's `date=`/`at=`/`d=`, resolve: what a single-scan endpoint (`/api/subtree`) reads. */
export async function resolveUnion(ctx: Ctx, view: UnionView, gate: boolean): Promise<{ resolved: Resolved; id: Identity | null } | Response> {
  const u = await openUnion(ctx, view, gate)
  if (u instanceof Response) return u
  const c = await cutoffOf(u, new URL(ctx.request.url).searchParams)
  if (c instanceof Response) return c
  return { resolved: membersAt(u, c), id: u.id }
}

/** A path's member: the one whose roots (at its resolved scan) hold the
 * path's first segment; null = none does (a 404). Two members holding it is
 * a misconfigured union (overlapping scopes), thrown. */
export async function memberOf(r: Resolved, path: string): Promise<Member | null> {
  const seg = path.split('/')[0]
  const ready = r.members.filter(m => m.date !== null)
  const roots = await Promise.all(ready.map(m => ivRetry(() => readRootRows(m.ctx.env, m.date!))))
  const owners = ready.filter((_, i) => (roots[i] ?? []).some(row => row.path === seg))
  if (owners.length > 1) throw new UnionOverlap(`union view '${r.view.key}': root '${seg}' is in stores ${owners.map(m => m.store).join(', ')}`)
  return owners[0] ?? null
}

/** Two members hold the same root: the union would double count it. */
export class UnionOverlap extends Error {}

/** A member's root rows (`readRootRows`): every top-level path at its scan, drawn or not. */
export type RootRow = { path: string; b: number; o: number; m?: number | null; k?: 'file' | 'dir' }

/** The union's root from its members' root trees (each read under the union's threshold): every member's
 *  top-level nodes, tagged with its store (`src`) and scan (`at`), largest first; the members' `(other)`s
 *  summed into one, last; the totals summed under `label`. A top-level name two members draw is a
 *  misconfigured union (`UnionOverlap`), never a sum.
 *
 *  With a member's `roots` (the plain root; never a filtered one, whose roots are its matches), every root the
 *  threshold folded into its `(other)` is listed too, out of the fold: the root's bucket table lists every
 *  member's every root, however small against the union's total (the treemap may still dust it). */
export function unionRoot(view: UnionView, label: string, members: readonly { store: string; date: string; tree: ViewNode; roots?: readonly RootRow[] | null }[]): ViewNode {
  const kids: ViewNode[] = []
  const other: ViewNode = { n: '(other)', k: 'dir', b: 0, o: 0, f: 0 }
  const seen = new Map<string, string>()
  for (const m of members) {
    for (const c of m.tree.c ?? []) {
      if (c.n === '(other)') {
        other.b += c.b; other.o += c.o; other.f = (other.f ?? 0) + (c.f ?? 0)
        continue
      }
      const prev = seen.get(c.n)
      if (prev) throw new UnionOverlap(`union view '${view.key}': root '${c.n}' is in stores ${prev}, ${m.store}`)
      seen.set(c.n, m.store)
      kids.push({ ...c, src: m.store, at: m.date })
    }
    const drawn = new Set((m.tree.c ?? []).map(c => c.n))
    for (const r of m.roots ?? []) {
      if (drawn.has(r.path)) continue
      const prev = seen.get(r.path)
      if (prev) throw new UnionOverlap(`union view '${view.key}': root '${r.path}' is in stores ${prev}, ${m.store}`)
      seen.set(r.path, m.store)
      kids.push({ n: r.path, k: r.k ?? 'dir', b: r.b, o: r.o, ...(r.m != null ? { d: Math.floor(r.m / 86400) } : {}), src: m.store, at: m.date })
      other.b -= r.b; other.o -= r.o
      if (other.f !== undefined) other.f -= 1
    }
  }
  if (other.f !== undefined && other.f <= 0) delete other.f
  kids.sort((x, y) => y.b - x.b || (x.n < y.n ? -1 : x.n > y.n ? 1 : 0))
  // Lifted roots leave the fold what the threshold folded beside them (nothing, at a bucket root); never below zero.
  other.b = Math.max(0, other.b); other.o = Math.max(0, other.o)
  if (other.b > 0 || other.o > 0) kids.push(other)
  return {
    n: label, k: 'dir',
    b: members.reduce((n, m) => n + m.tree.b, 0), o: members.reduce((n, m) => n + m.tree.o, 0),
    ...(kids.length ? { c: kids } : {}),
  }
}

/** A member's refusal of a filter (`q=`) for its mount: its own `{error, code}` (`indexedOnly.ts`), e.g.
 *  `scan-not-indexed` for a store with no static name index of its own. */
export interface Refusal { store: string; code: string; error: string }

/** A member's refusal, if its response is one: a 400 whose JSON body carries a `code`. Null otherwise (an answer,
 *  or any other failure, which the caller passes on as is). */
export async function refusalOf(store: string, res: Response): Promise<Refusal | null> {
  if (res.status !== 400 || !res.headers.get('content-type')?.startsWith('application/json')) return null
  const b = await res.clone().json() as { error?: unknown; code?: unknown }
  return typeof b.code === 'string' && typeof b.error === 'string' ? { store, code: b.code, error: b.error } : null
}

/** A refusal as its member answered it (the body `rejectBody` writes). */
export const refusalResponse = (r: Refusal): Response =>
  new Response(JSON.stringify({ error: r.error, code: r.code }), { status: 400, headers: { 'content-type': 'application/json' } })

/** The `union` block of a filtered answer: the vector, each refusing member's refusal beside its scan. */
export const filteredVector = (r: Resolved, refused: readonly Refusal[]) => {
  const v = vectorOf(r)
  return { ...v, sources: v.sources.map(s => { const x = refused.find(f => f.store === s.store); return x ? { ...s, refused: { code: x.code, error: x.error } } : s }) }
}

type Count = { n: number; b: number; o: number }
type Interiors = { read: number; skipped: number; reason: string; late?: number }
type RollupInfo = { children: number; kept: number; rows: number | null; bucketsOnly?: true; scopedBelow?: true }

/** The filter fields of a member's `/api/subtree?q=` body that a union root merges. */
export interface FilteredRoot {
  tree: ViewNode
  matches?: string[]
  matched?: { path: string; b: number; o: number }[]
  matchCount?: Count
  matchesCapped?: true
  rollup?: RollupInfo
  excluded?: string[]
  firstPaint?: true
  interiors?: Interiors
  partialReason?: string
  approximateReason?: string
  dirsOnly?: true
  hexRuns?: { min: number; tail: number }
}

const sumOf = <T>(xs: readonly T[], f: (x: T) => number): number => xs.reduce((n, x) => n + f(x), 0)
const named = (store: string, reason: string): string => `${store}: ${reason}`

/**
 * A union root's filtered answer (`q=`): every answering member's filtered root (each read under the union's one
 * threshold), merged as the plain root merges — the tree by `unionRoot`, the match lists concatenated (each
 * member's paths are under its own roots), `matchCount` and the rollup counts summed, a flag set when any member
 * set it. A member that refused the filter for its mount (`refused`) contributes nothing and makes the answer
 * partial, named in `partialReason` (`<store>: <why>`, as each member's own partial reason is) — never a zero
 * for its mount.
 */
export function unionFiltered(view: UnionView, label: string, members: readonly { store: string; date: string; body: FilteredRoot }[], refused: readonly Refusal[]): Omit<FilteredRoot, 'tree'> & { tree: ViewNode; partial?: true; approximate?: true } {
  const bodies = members.map(m => m.body)
  const counts = bodies.flatMap(b => (b.matchCount ? [b.matchCount] : []))
  const rollups = bodies.flatMap(b => (b.rollup ? [b.rollup] : []))
  const interiors = bodies.flatMap(b => (b.interiors ? [b.interiors] : []))
  const partial = [...members.flatMap(m => (m.body.partialReason ? [named(m.store, m.body.partialReason)] : [])), ...refused.map(r => named(r.store, r.error))]
  const approximate = members.flatMap(m => (m.body.approximateReason ? [named(m.store, m.body.approximateReason)] : []))
  const late = interiors.filter(i => i.late !== undefined)
  const hex = bodies.find(b => b.hexRuns)?.hexRuns
  return {
    ...(hex ? { hexRuns: hex } : {}),
    matches: bodies.flatMap(b => b.matches ?? []).sort(),
    matched: bodies.flatMap(b => b.matched ?? []).sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)),
    ...(counts.length ? { matchCount: { n: sumOf(counts, c => c.n), b: sumOf(counts, c => c.b), o: sumOf(counts, c => c.o) } } : {}),
    ...(bodies.some(b => b.matchesCapped) ? { matchesCapped: true as const } : {}),
    ...(rollups.length ? { rollup: {
      children: sumOf(rollups, r => r.children), kept: sumOf(rollups, r => r.kept),
      rows: rollups.some(r => r.rows === null) ? null : sumOf(rollups, r => r.rows!),
      ...(rollups.some(r => r.bucketsOnly) ? { bucketsOnly: true as const } : {}),
      ...(rollups.some(r => r.scopedBelow) ? { scopedBelow: true as const } : {}),
    } } : {}),
    ...(bodies.some(b => b.excluded) ? { excluded: bodies.flatMap(b => b.excluded ?? []).sort() } : {}),
    ...(bodies.some(b => b.firstPaint) ? { firstPaint: true as const } : {}),
    ...(interiors.length ? { interiors: {
      read: sumOf(interiors, i => i.read), skipped: sumOf(interiors, i => i.skipped),
      reason: [...new Set(interiors.map(i => i.reason))].join(' · '),
      ...(late.length ? { late: sumOf(late, i => i.late!) } : {}),
    } } : {}),
    ...(partial.length ? { partial: true as const, partialReason: partial.join(' · ') } : {}),
    ...(approximate.length ? { approximate: true as const, approximateReason: approximate.join(' · ') } : {}),
    ...(bodies.some(b => b.dirsOnly) ? { dirsOnly: true as const } : {}),
    tree: unionRoot(view, label, members.map(m => ({ store: m.store, date: m.date, tree: m.body.tree }))),
  }
}
