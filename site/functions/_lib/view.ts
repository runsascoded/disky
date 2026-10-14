/** One view = "path + scan + scope + pixel budget", answered from the index
 * tiers (specs/view-serving.md §1–2). Shared by `/api/subtree` (the map),
 * `/api/todo` (the review backlog) and anything else that wants a folded tree.
 *
 * Treemap area is proportional to bytes, so "everything this canvas can draw
 * under P" is one predicate: b ≥ P.b · minArea / (w·h), attenuated per level.
 * Every tier holds rows `(path, depth, usr, b, o, …)`, descendant-
 * inclusive, sorted `(depth, path)` (or by usr for a user lens), so a query
 * is row-group-pruned range reads, a threshold filter, and a nested-tree
 * assembly with `(other)` = parent − Σ kept children (exact by subtraction).
 *
 * Tier planning, version-1 generations (dir rows only): the coarse tiers
 * keep only paths whose subtree clears an absolute floor F_E (the job wrote
 * E = 16, 20, 24). Descendant-inclusive bytes are monotone, so when the
 * query's threshold is ≥ F_E the tier's rows are a superset of what the
 * query keeps and the answer is identical to the floor-free tier's — just
 * read from a few row groups instead of the whole file. The planner picks
 * the coarsest such tier; a path below every floor (or a scan without
 * coarse tiers) reads the floor-free tier.
 *
 * Sort choice, version-2 generations (the path store, specs/path-store.md:
 * every row, objects included, `kind` on each, no coarse tiers): a
 * thresholded subtree read goes to the `bysize` sort — the groups above the
 * threshold under P, whatever the tree's shape — unless P's subtree is small
 * enough (`n_desc` under `SMALL_SUBTREE_ROWS`) that the fixed per-bucket
 * cost outweighs it, in which case `path` is read whole and thresholded in
 * memory. Point lookups (P's own row, the diff's other-side names) always
 * read `path`. `(other)` = P − Σ kept on both counts and bytes, with
 * `f = n_children(P) − kept` — objects and dirs alike.
 */
import type { Env } from './auth.js'
import { asOfScans, folded, type IndexHandle, isStore, ivLastRead, type Lens, openIndex, pathStoreKey, perScan, planRects, slices, planSizeRects, readAsks, readRects, readRows, readRootHistoryRows, readSizeRects, type Rect, type Row, sizeVariant, type Span, TooWide, type Trace, withTrace } from './index.js'
import { type FoldedLens, ownerLens, poolLens } from './owners.js'
import { type ClassScope, classRow, nameFilter, type NamePred, ownerKey, ownerOk, type OwnerScope } from './scope.js'
import { filterThreshold, looseThreshold, matchRoots, pickTier, rebasedThreshold, rootRects } from './filter.js'
import { type SearchFound, type SearchLimits, searchRoots } from './search.js'
import { planNegative, planPositive } from './searchQuery.js'
import { hasLedger } from './ledger.js'
import { ownerAssignments } from './ownerTotals.js'
import { shared } from './shared.js'
import { storeKey } from './stores.js'
import { extrasFor } from './extras.js'
import { loadRegistry } from './identity.js'
import { covers, declined, dirsOnlyOf, dirsOnlySplit, type Found, type Hit, type HitOpts, type Rollup, rollupAt, staticFilterStore, staticKey, staticLiteral } from './staticFilter.js'
import { scanAt } from './staticNames.js'
import { FilterRejected, indexedOnly, reject } from './indexedOnly.js'

export const MIN_AREA_DEFAULT = 12 // px² of the smallest legible cell (~3×4)
// Each nesting level below the query root loses canvas to chrome (title bars,
// insets) — and deeper detail is one drill away regardless. The per-node
// threshold doubles per level: thr(depth) = thr · ATTEN^(depth − dP − 1).
export const ATTEN_DEFAULT = 2
export const QUANT = 128 // px quantization for w/h → cache-key stability
const HARD_CAP = 50_000 // response nodes; way above any real canvas budget
// Coarse tiers the version-1 job wrote, coarsest first (viz.py COARSE_EXPS).
const COARSE_EXPS = [16, 20, 24]
/** A store subtree with at most this many rows reads `path` without planning
 * `bysize` too. 0: always plan both — a subtree's `path` read decodes ~one
 * group per depth whatever its `n_desc`, so a row count can't say which sort
 * is cheaper (gcs's 32K-row groups: a 2K-row dir 20 levels deep decoded
 * ~1.3M rows from `path`). The two span queries cost ~20 ms. */
export const SMALL_SUBTREE_ROWS = 0
/** On the interval store, a subtree with more rows than this (or an unknown count) reads `bysize`
 * without planning `path` (`planSubtree`). */
export const IV_PATH_PLAN_ROWS = 1 << 18
/** On the interval store, a depth-band read (every rect depth-bounded) takes `path` when its plan holds
 *  at most this many rows (`planSubtree`). */
export const IV_BAND_ROWS = 1 << 16
/** A v1 floor-free tier is sorted `(depth, path)`, so a size threshold can't
 * prune it: the filter's no-match fallback reads every row under the path.
 * For a whole bucket that read exceeds the Worker's limits (a bucket-level
 * filter on 2026-10-01 died with 1102 and took its isolate's other requests
 * with it), so past this many objects under the path (v1 rows carry no row
 * count; a subtree's dirs are far fewer than its objects) the fallback is
 * skipped and the matches are the coarse tiers'. A 390k-object subtree
 * read in 5–8 s. */
export const V1_FILTER_SCAN_OBJECTS = 500_000

/** A node of the served tree. `k`: what the path is — an object (`file`) or
 * a directory; a v1 generation only has directories, and a fold (`(other)`)
 * is drawn as a branch. */
export type ViewNode = { n: string; k: 'file' | 'dir'; b: number; o: number; c?: ViewNode[]; f?: number } & Record<string, unknown>

export interface ViewOpts {
  date: string
  path: string
  /** Timing sink for the response's `Server-Timing` header (see `index.ts` `Trace`). */
  trace?: Trace
  w: number
  h: number
  minArea: number
  atten: number
  lens?: Lens
  /** Absolute byte threshold instead of the pixel-budget one (e.g. the to-do
   * backlog's min bytes). Still attenuated per level by `atten`. */
  threshold?: number
  // --- the page's scope axes (specs/view-serving.md §2) -------------------
  /** `o=owned|unowned`: the owner axis's pools (a user is `lens`). */
  owner?: OwnerScope
  /** `cl=` ⊆ snca: keep only these storage classes' bytes (rows are scaled,
   * not picked — see `classRow`). */
  classes?: ClassScope
  /** `by=<assigner>`: with a user `lens`, fold only the assignments that assigner
   * made (the `/assignments` heatmap's cell → "the part of <to>'s estate that
   * <by> assigned"). A canonical user id; kept off `Lens` so index tier reads
   * (keyed by the lens user) are unaffected — only the assignments fold narrows. */
  by?: string
  /** `depth=N`: read only N levels below the root (rows at the cap arrive
   * without children — still drillable branches). The same pixel budget, so
   * the top level is *identical* to the uncapped view's; a `depth=1` fetch is
   * one depth-band read that the client shows while the full tree loads, and
   * the full tree then fills in under tiles that don't move. */
  maxDepth?: number
  /** `q=`: name filter over the read rows' paths (see `scope.ts`). */
  query?: NamePred
  /** With `query`: the fast first paint — the forest read from the coarsest
   * tier regardless of the budget (specs/filter-views.md §2). The client
   * re-requests with `full=1` for the planned tiers. */
  firstPaint?: boolean
  /** The store's small-subtree cutoff, rows (default `SMALL_SUBTREE_ROWS`). */
  smallRows?: number
  /** The interval store's cutoff past which a subtree plans `bysize` alone (`IV_PATH_PLAN_ROWS`; tests
   *  lower it to reach that read on small fixtures). */
  ivPlanRows?: number
  /** With `query` on a v1 scan: the largest subtree (objects) the no-match
   * fallback reads (default `V1_FILTER_SCAN_OBJECTS`). */
  v1ScanObjects?: number
  /** The search index's read budgets (default `SEARCH_LIMITS`). */
  searchLimits?: SearchLimits
  /** With `query`: the most nodes the tree draws (default `tileBudget(w, h)`). */
  maxTiles?: number
  /** With `query`: phase 2's row-group and row budgets (default `FILTER_PHASE2_GROUPS`, `FILTER_PHASE2_ROWS`). */
  phase2Groups?: number
  phase2Rows?: number
  /** With `query`: only the filter's floor (`Read.threshold`, from the match roots' total) — no forest. */
  floorOnly?: boolean
  /** With `query`: ms the static roots' detail lookups may run past phase 2 (default `FILTER_DETAILS_MS`). */
  detailsWait?: number
  /** With `query`: phase 2's time budget, ms (default `FILTER_PHASE2_MS`, env or constant). */
  phase2Ms?: number
  /** With `query`: the request's phase-2 gate (`Phase2Gate`), shared by every read of one request. */
  phase2Gate?: Phase2Gate
  /** A heavy literal's rollup view (`rollupRead`): paths to nest whatever their bytes and the read budget — the
   *  ones a diff's other side answered (`Read.nested`), so both sides know the same paths whole. */
  nestForce?: Set<string>
  /** …and the most answers it nests (default `FILTER_NEST_READS`). */
  nestReads?: number
}

/** One request's phase 2 across its reads (a diff's two sides, its re-read at the shared floor): once a round
 *  reads nothing because its time budget ran out (`dead`), the request's later rounds skip phase 2 outright —
 *  its D1 span plans were the whole cost and drew nothing (gcs 10-09 `nemotron`: `read:0`, 48 of 48 late). */
export interface Phase2Gate { dead: boolean }

export interface View {
  tree: ViewNode
  /** Which tier answered: a v1 generation's `coarse<E>` or `fine`
   * (floor-free); a store generation's sort, `bysize` or `path` (a lens:
   * `bysize-user` / `user`). */
  tier: string
  /** How that tier's metadata was read: `d1` (footer in D1) or `footer`. */
  index: string
  threshold: number
  nodes: number
  truncated: boolean
  /** With `query`: the outermost matching paths (what a bulk action targets). */
  matches?: string[]
  /** With `query`: each match root's scoped total (the series / subtitles),
   * net of its excluded descendants (NOT). */
  matched?: { path: string; b: number; o: number }[]
  /** With a NOT query: the outermost excluded paths under the match roots
   * (subtracted from their roots' totals, absent from the tree). */
  excluded?: string[]
  /** With `query`: every match root's count and summed bytes / objects — `matches` and `matched` list
   * at most `MATCH_LIST_CAP` of them (the tree's own roots always, then the heaviest), `matchesCapped`
   * when they leave some out. */
  matchCount?: { n: number; b: number; o: number }
  matchesCapped?: true
  /** With `query`, a heavy literal under a heavy directory (`staticDrill.ts`): the view is P's rollup —
   * its children's matched totals (the `children` holding match roots on any scan, `kept` of them by name,
   * the rest in `(other)`), not its match roots. The tree's top-level cells are those children, the drawn ones
   * nested in turn (`rollupRead`: each one's own rollup or match roots, to `FILTER_SUBDIV_LEVELS` below P);
   * `matched` lists only the match roots those answers name, so `matchesCapped` is set, and `matchCount.n` is `rows`, the root rows under P over every scan (an upper
   * bound on this scan's; at the fleet root a long literal's alias entry, a 1–2 character one's counted from
   * the roots index). `bucketsOnly`: the fleet root from the catalog with no drilldown (`FILTER_STATIC_HEAVY`
   * off) — bucket tiles, exact bytes and objects, `rows` null (so `matchCount.n` counts only the matched
   * buckets); a bucket's view is refused (`term-too-common`) — unless `scopedBelow` (`^q`'s starts-with catalog), where
   * a view below answers when the term's rows there are few enough. */
  rollup?: { children: number; kept: number; rows: number | null; bucketsOnly?: true; scopedBelow?: true }
  /** With `query`: read from the coarsest tier for the first paint. */
  firstPaint?: boolean
  /** With `query`: phase 2 left `skipped` of the roots big enough to subdivide undivided — drawn as one
   *  exact tile each — past its read or time budget (`reason`; `late` of them past the time budget, which a
   *  retry may not hit: such an answer is kept only `PARTIAL_TTL` s, colo-only, and says `budgetCut`);
   *  `read` were subdivided. Absent: every
   *  such root was. Totals are exact regardless. */
  interiors?: { read: number; skipped: number; reason: string; late?: number }
  /** With `query`: a read budget stopped the search — some matches may be
   * missing (`partialReason` says why). Never silent. */
  partial?: true
  partialReason?: string
  /** With `query`: the matches came from a thresholded read, not the search
   * index — small ones may be missing (`approximateReason` says why). */
  approximate?: true
  approximateReason?: string
  /** With `query`: the scan lists folders only (a v1 index, `DIRS_ONLY`): the matches are its folder-name
   *  matches, exact as such; a file name is never matched there. */
  dirsOnly?: true
}

/** Whether a filtered read may be missing matches, and why (the response's
 * `partial` / `approximate`): every way the filter view can see fewer
 * matches than exist sets one. */
export interface Coverage {
  partial?: string[]
  approximate?: string[]
  /** The scan is a dir-only (v1) one (`staticRuns.ts` `dirOnlyScans`): its matches are its folders' names
   *  only — exact as such, but never the literal's whole answer. */
  dirsOnly?: string[]
}
export const APPROX_NO_INDEX = 'this scan has no search index; small matches may be missing'
export const APPROX_V1_TOO_BIG = 'this scan has no search index and this view is too big to scan; small matches may be missing'
export const APPROX_UNINDEXED = 'this query can’t use the search index; small matches may be missing'
export const APPROX_LENS = 'a user lens filters only the rows it read; small matches may be missing'
export const APPROX_EXCL_NO_INDEX = 'this scan has no search index; small exclusions may be missed'
export const DIRS_ONLY = 'this scan lists folders only — files aren’t searchable on it'
export const APPROX_EXCL_UNINDEXED = 'this query’s exclusions can’t use the search index; small ones may be missed'
/** Add a reason (once) to a coverage flag. */
export function noteCoverage(cov: Coverage, flag: keyof Coverage, why: string | undefined): void {
  if (!why) return
  const have = (cov[flag] ??= [])
  if (!have.includes(why)) have.push(why)
}
/** A coverage as response fields. */
export const coverageFields = (c: Coverage): Pick<View, 'partial' | 'partialReason' | 'approximate' | 'approximateReason' | 'dirsOnly'> => ({
  ...(c.partial ? { partial: true as const, partialReason: c.partial.join(' · ') } : {}),
  ...(c.approximate ? { approximate: true as const, approximateReason: c.approximate.join(' · ') } : {}),
  ...(c.dirsOnly ? { dirsOnly: true as const } : {}),
})

export class NotFound extends Error {}
/** The scan has no synced index for this lens (older scans) — a client-side
 * fallback question, not a server fault. */
export class LensUnavailable extends Error {}

/** A path's aggregate over its rows (owner slices) — the wire's names. */
interface Agg {
  b: number
  o: number
  /** Σ size·mtime_mean over rows with a mean, and the bytes weighed. */
  wts: number
  wb: number
  a: number | null // subtree-max last-read epoch day (read lens); null = never read
  cb: Record<string, number>
  ub: Record<string, number>  // per-user bytes; unowned = b − Σ ub
  /** What the path is; null on a synthesized aggregate (a fold, a filtered ancestor). */
  kind: 'file' | 'dir' | null
  /** Direct children, objects + dirs — the same on every slice of a path;
   * null on a v1 row, which never had it. */
  nc: number | null
  /** All descendants (rows under the path in the `path` sort); null on v1. */
  nd: number | null
  /** Bytes by age bucket (`Row.ages`), summed over rows; null when none had them. */
  ag: number[] | null
  /** Built from exactly one interval-store row: that version's `[vf, vt)` (null once a second row
   *  merges in; absent on per-scan rows and synthesized aggregates). A diff's lookups use it. */
  ver?: { vf: number; vt: number } | null
}

const newAgg = (): Agg => ({ b: 0, o: 0, wts: 0, wb: 0, a: null, cb: {}, ub: {}, kind: null, nc: null, nd: null, ag: null })

function merge(a: Agg, r: Row): void {
  a.b += r.size
  a.o += r.n_files
  if (r.mtime_mean != null && r.mtime_w > 0) {
    a.wts += r.mtime_mean * r.mtime_w
    a.wb += r.mtime_w
  }
  if (r.last_read != null) a.a = a.a == null ? r.last_read : Math.max(a.a, r.last_read)
  if (r.cls2) a.cb['2'] = (a.cb['2'] ?? 0) + r.cls2
  if (r.cls3) a.cb['3'] = (a.cb['3'] ?? 0) + r.cls3
  if (r.cls4) a.cb['4'] = (a.cb['4'] ?? 0) + r.cls4
  if (r.usr) a.ub[r.usr] = (a.ub[r.usr] ?? 0) + r.size
  if (r.us) for (const u in r.us) a.ub[u] = (a.ub[u] ?? 0) + r.us[u]
  a.kind = r.kind
  if (r.n_children != null) a.nc = r.n_children
  if (r.n_desc != null) a.nd = r.n_desc
  if (r.ages) a.ag = a.ag ? a.ag.map((v, i) => v + r.ages![i]) : [...r.ages]
  if (r.vf != null && r.vt != null) a.ver = a.ver === undefined ? { vf: r.vf, vt: r.vt } : null
}

function subtract(parent: Agg, kids: Agg[]): Agg {
  const out = newAgg()
  out.a = parent.a // max isn't subtractable; the residual's read day ≤ parent's
  const sum = (f: (a: Agg) => number) => kids.reduce((s, k) => s + f(k), 0)
  out.b = parent.b - sum(a => a.b)
  out.o = Math.max(0, parent.o - sum(a => a.o))
  out.wts = parent.wts - sum(a => a.wts)
  out.wb = Math.max(0, parent.wb - sum(a => a.wb))
  if (parent.ag) out.ag = parent.ag.map((v, i) => Math.max(0, v - kids.reduce((s, k) => s + (k.ag?.[i] ?? 0), 0)))
  for (const key of ['cb', 'ub'] as const) {
    for (const [k, v] of Object.entries(parent[key])) {
      const r = v - kids.reduce((s, kid) => s + (kid[key][k] ?? 0), 0)
      if (r > 0) out[key][k] = r
    }
  }
  return out
}

/** Scale an aggregate to `frac` of itself — a scope's share of a node: bytes
 * exactly, the rest (objects, per-user/class splits, written-time weights)
 * proportionally, which assumes the scope is spread like the node's mix. */
function scale(a: Agg, frac: number): Agg {
  if (frac === 1) return a
  const out = newAgg()
  out.b = a.b * frac
  out.o = a.o * frac
  out.wts = a.wts * frac
  out.wb = a.wb * frac
  out.a = a.a
  out.kind = a.kind
  out.nc = a.nc
  out.nd = a.nd
  out.ag = a.ag && a.ag.map(v => v * frac)
  for (const key of ['cb', 'ub'] as const) for (const [k, v] of Object.entries(a[key])) out[key][k] = v * frac
  return out
}

function display(a: Agg): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (a.wb) out.d = Math.round(a.wts / a.wb / 86400)
  if (a.a != null) out.a = a.a
  if (a.ag?.some(v => v > 0)) out.ag = a.ag.map(v => Math.round(v))
  const desc = (m: Record<string, number>) =>
    Object.fromEntries(Object.entries(m).sort((x, y) => y[1] - x[1]))
  if (Object.keys(a.cb).length) out.cb = desc(a.cb)
  if (Object.keys(a.ub).length) {
    out.us = Object.entries(a.ub).sort((x, y) => y[1] - x[1]).map(([u, b]) => [u, b])
  }
  return out
}

/** Open an index variant; `null` when the scan has no such tier synced.
 * A miss is remembered for as long as a handle is (`openIndex`'s TTL): a
 * store generation has no coarse tiers and a v1 one no `bysize`, so every
 * view of it would otherwise re-ask D1 for the tiers it will never have. */
const missing = new Map<string, number>()
const MISS_TTL = 60_000
async function tryOpen(env: Env, date: string, variant: string): Promise<IndexHandle | null> {
  // Keyed by path store too: the interval store's read of a held v1 date finds no coarse tier (its
  // `path` sort is a store sort), which says nothing of the per-scan generation's.
  const ck = `${storeKey(env)}:${pathStoreKey(env)}:${date}:${variant}`
  const at = missing.get(ck)
  if (at != null && Date.now() - at < MISS_TTL) return null
  // Coarse tiers and the `user` sort are version-1 artifacts: when the date's
  // `path` sort is a store generation, any such pointer left for that date is
  // an earlier generation's (index-sync now retires them; older D1s still hold
  // some) and must not answer.
  if (variant.startsWith('coarse')) {
    const path = await tryOpen(env, date, 'path')
    if (path && isStore(path)) {
      missing.set(ck, Date.now())
      return null
    }
  }
  try {
    const h = await openIndex(env, date, variant)
    // A user-first sort left by an earlier v1 generation of a store date
    // (index-sync now retires it) must not answer a lens; a store
    // generation's own (`path-index -u bysize`) does.
    if ((variant === 'user' || variant === 'bysize-user') && !isStore(h)) {
      const path = await tryOpen(env, date, 'path')
      if (path && isStore(path)) {
        missing.set(ck, Date.now())
        return null
      }
    }
    return h
  } catch (e) {
    if (String((e as Error).message).includes('not synced')) {
      missing.set(ck, Date.now())
      return null
    }
    throw e
  }
}

/** A thresholded read of everything under some rects, from the sort that
 * answers it cheapest (module doc): a store generation's `bysize` twin of
 * `pathIdx`'s sort for a big subtree, else `pathIdx` itself (a v1 tier, or a
 * small store subtree read whole). `nDesc` = rows under P (null = unknown:
 * treat as big). Returns the rows and the variant that served them. */
async function readSubtree(
  env: Env,
  date: string,
  pathIdx: IndexHandle,
  rects: Rect[],
  thrAt: (depth: number) => number,
  lens: Lens | undefined,
  nDesc: number | null,
  smallRows: number,
  tr?: Trace,
  ivPlanRows = IV_PATH_PLAN_ROWS,
): Promise<{ rows: Row[]; variant: string }> {
  const plan = await planSubtree(env, date, pathIdx, rects, thrAt, lens, nDesc, smallRows, tr, false, ivPlanRows)
  return { rows: await plan.read(), variant: plan.variant }
}

/** `readSubtree`'s plan, before any group is fetched: the sort it would read, how many row groups and
 *  rows that read decodes, and the read itself — so a caller can hold reads to a group budget. */
interface SubtreePlan { variant: string; groups: number; rows: number; read: (stop?: () => boolean) => Promise<Row[]> }

async function planSubtree(
  env: Env,
  date: string,
  pathIdx: IndexHandle,
  rects: Rect[],
  thrAt: (depth: number) => number,
  lens: Lens | undefined,
  nDesc: number | null,
  smallRows: number,
  tr?: Trace,
  pathOnly = false,
  ivPlanRows = IV_PATH_PLAN_ROWS,
): Promise<SubtreePlan> {
  const held = (plan: Span[]) => plan.reduce((n, x) => n + (x.rowEnd - x.rowStart), 0)
  const of = (variant: string, plan: Span[], read: SubtreePlan['read']): SubtreePlan => ({ variant, groups: plan.length, rows: held(plan), read })
  // A store generation: plan the read on both sorts (span queries only, no
  // decode) and decode whichever holds fewer rows. The cost of a `path` read
  // is ~one group per depth of the subtree, of a `bysize` read ~one group per
  // size bucket above the threshold — neither predicts the other from
  // `n_desc` alone, and with large row groups the wrong pick decodes 4×
  // more (gcs at 32K-row groups: small drills over the 700K-row cap).
  // `smallRows`: below it the `path` read is taken without planning `bysize`.
  // The interval store plans from footer groups it must fetch and decode (no D1): a big subtree's
  // `path` plan touches most of them, and `bysize` is the cheaper read there anyway, so it isn't planned.
  // A lens reads the user-first size sort, as below: the path-first one (`slices-bytotal`) mixes users
  // in every group, and a big user's root there selected most groups over the threshold.
  // A depth band (`depth=N`, a capped diff's views) is the exception: `path` holds a depth's rows under P
  // together, so its plan touches the few footer groups of those depths and reads a group or two per depth
  // (per tier), where `bysize` reads a group per size bucket of every segment (gcs `marin-us-central2`
  // at depth 1: 3 groups against 24). Taken when it holds at most `IV_BAND_ROWS`; past that (a flat
  // directory's children) the size sort is planned too and the one holding fewer rows read.
  if (!pathOnly && pathIdx.asOf != null && (nDesc == null || nDesc > ivPlanRows)) {
    const pp = !lens && rects.every(q => q.dHi < 1e9) ? await planRects(pathIdx, rects, thrAt) : null
    if (pp && held(pp) <= IV_BAND_ROWS) return of(pathIdx.variant, pp, stop => readRects(pathIdx, rects, thrAt, lens, pp, stop))
    const sized = (lens ? await tryOpen(env, date, 'bysize-user') : null) ?? await tryOpen(env, date, 'bysize')
    if (sized) {
      const sh = withTrace(sized, tr)
      const sp = await planSizeRects(sh, rects, thrAt, lens)
      if (pp && held(pp) < held(sp)) return of(pathIdx.variant, pp, stop => readRects(pathIdx, rects, thrAt, lens, pp, stop))
      return of(sized.variant, sp, stop => readSizeRects(sh, rects, thrAt, lens, sp, stop))
    }
  }
  if (!pathOnly && isStore(pathIdx) && (nDesc == null || nDesc > smallRows)) {
    // A lens prefers the user-first size sort where the scan has one (gcs
    // writes only `bysize-user`): a user's root reads their own groups.
    const sized = (lens ? await tryOpen(env, date, 'bysize-user') : null) ?? await tryOpen(env, date, sizeVariant(pathIdx.variant))
    if (sized) {
      const sh = withTrace(sized, tr)
      const [pp, sp] = await Promise.all([planRects(pathIdx, rects, thrAt, lens), planSizeRects(sh, rects, thrAt, lens)])
      if (held(sp) < held(pp)) return of(sized.variant, sp, stop => readSizeRects(sh, rects, thrAt, lens, sp, stop))
      return of(pathIdx.variant, pp, stop => readRects(pathIdx, rects, thrAt, lens, pp, stop))
    }
  }
  const pp = await planRects(pathIdx, rects, thrAt, lens)
  return of(pathIdx.variant, pp, stop => readRects(pathIdx, rects, thrAt, lens, pp, stop))
}

/** Rows under P, from P's own rows: `n_desc` (the same on every owner
 * slice); the fleet root sums its depth-1 rows' subtrees (+ the rows
 * themselves). Null on a v1 generation. */
function subtreeRows(path: string, rootRows: Row[]): number | null {
  if (path !== '') return rootRows[0]?.n_desc ?? null
  const per = new Map<string, number | null>()
  for (const r of rootRows) per.set(r.path, r.n_desc)
  let n = 0
  for (const nd of per.values()) {
    if (nd == null) return null
    n += nd + 1
  }
  return n
}

const floorOf = (h: IndexHandle): number | null => h.floor

/** Just P's scoped aggregate for one scan — the size-over-time chart's point
 * (`/api/series`): the root read of a view, from the coarsest tier that has
 * P, without folding anything under it. */
/** The store root's depth-1 rows (one per bucket) in a scan — `/api/series
 * ?split=roots` (specs/done/root-geneses.md §1); null = the scan has no tier. The
 * same one range read `readRootAgg` sums for the unscoped root. */
export async function readRootRows(env: Env, date: string): Promise<{ path: string; b: number; o: number }[] | null> {
  const top = await tryOpen(env, date, `coarse${COARSE_EXPS[0]}`)
  const rows = top ? await readRows(top, 1, 1, '', '￿') : []
  const src = rows.length ? rows : await (async () => { const fine = await tryOpen(env, date, 'path'); return fine ? readRows(fine, 1, 1, '', '￿') : null })()
  if (src == null) return null
  const by = new Map<string, { path: string; b: number; o: number }>()
  for (const r of src) {
    const a = by.get(r.path) ?? { path: r.path, b: 0, o: 0 }
    a.b += r.size; a.o += r.n_files
    by.set(r.path, a)
  }
  return [...by.values()]
}

export async function readRootAgg(env0: Env, o: { date: string; path: string; lens?: Lens; owner?: OwnerScope; by?: string; classes?: ClassScope }, historyRows?: Row[]): Promise<{ b: number; o: number } | null> {
  if (historyRows && o.path) throw new Error('history rows are for the fleet root only')
  const env = sliced(env0, o)
  const { date, path, lens, owner, classes } = o
  const dP = path === '' ? 0 : path.split('/').length
  const readRoot = (idx: IndexHandle, l?: Lens) =>
    path === '' ? readRows(idx, 1, 1, '', '￿', undefined, l) : readRows(idx, dP, dP, path, path, undefined, l)
  // P's row from the coarsest tier of `sort`, else the floor-free one; null
  // = no tier. Two hops at most: a point read costs the same few round trips
  // on any tier, so walking every coarse tier down only made a small user's
  // (or an assignments-only user's) series four hops per scan.
  const rootRows = async (sort: string, l?: Lens): Promise<Row[] | null> => {
    const top = await tryOpen(env, date, `coarse${COARSE_EXPS[0]}${sort === 'path' ? '' : `-${sort}`}`)
    if (top) {
      const rows = await readRoot(top, l)
      if (rows.length) return rows
    }
    const fine = await tryOpen(env, date, sort)
    return fine ? readRoot(fine, l) : null
  }
  const ol = lens ? await ownerLensFor(env, date, lens, o.by) : null
  const rows = historyRows ? historyRows.filter(r => !lens || r.usr === lens.key) : await rootRows(lens ? await lensSort(env, date) : 'path', lens)
  // No tier at all → null. No rows for an *unlensed* path (its `path`-tier read
  // came back empty) → the path isn't in this scan, so null (a gap on the
  // over-time chart — e.g. a bucket's scans before it joined the scan set),
  // distinct from an owner/class slice that filters an existing path to zero (a
  // real 0, kept — those rows are non-empty). A lens reads a pre-filtered `user`
  // tier where empty can't tell "absent" from "owns nothing", so it's unchanged.
  if (rows == null || (!lens && rows.length === 0)) return null
  const mine = newAgg()
  for (const r of rows) if (ownerOk(r.usr, owner)) merge(mine, classRow(r, o.classes))
  const pl = lens ? null : await poolLensFor(env, date, owner)
  if (pl) {
    const all = newAgg()
    for (const r of rows) merge(all, classRow(r, o.classes))
    const agg = poolAgg(pl, owner!, path, all, mine)
    return { b: agg.b, o: agg.o }
  }
  if (!ol) return { b: mine.b, o: mine.o }
  let all: Agg | null = null
  if (ol.needsTotal(path)) {
    const allRows = historyRows ?? await rootRows('path')
    if (allRows?.length) { all = newAgg(); for (const r of allRows) merge(all, r) }
  }
  const agg = lensAgg(ol, lens!.key, path, all, mine)
  return { b: agg.b, o: agg.o }
}

/** Held dates' root totals from one version read, with the canonical per-date
 * ledger fold. No new index, inferred object counts, or snapshot-only owners. */
export async function readRootHistory(env: Env, dates: string[], scope: { lens?: Lens; owner?: OwnerScope }): Promise<Map<string, { b: number; o: number } | null> | null> {
  const rows = await readRootHistoryRows(env, dates, scope.lens)
  if (!rows) return null
  const out = new Map<string, { b: number; o: number } | null>()
  const held = [...rows.keys()]
  for (let i = 0; i < held.length; i += 12) {
    const points = await Promise.all(held.slice(i, i + 12).map(async date => [date, await readRootAgg(env, { date, path: '', ...scope }, rows.get(date)!)] as const))
    for (const [date, point] of points) out.set(date, point)
  }
  return out
}

/** The owner lens for a scan: the live assignments folded against it (cached per
 * (scan, ledger head) by the totals machinery) — null when nothing is assigned. */
const assignerMemo = new Map<string, Promise<Map<string, string>>>()
/** email → canonical user id (`user_emails`), memoized per isolate — to match
 * an assignment's assigner (`actions.actor`, an email) against a `by=` (canonical). */
async function assignerMap(env: Env): Promise<Map<string, string>> {
  return shared(assignerMemo, 'ue', async () => {
    const m = new Map<string, string>()
    const ue = await env.DB!.prepare('SELECT email, user FROM user_emails').all<{ email: string; user: string }>()
    for (const r of ue.results) m.set(r.email.toLowerCase(), r.user)
    return m
  }, 10_000)
}
async function ownerLensFor(env: Env, date: string, lens: Lens, by?: string): Promise<FoldedLens | null> {
  let assignments = await ownerAssignments(env, date)
  if (by) {
    const emap = await assignerMap(env)
    assignments = assignments.filter(c => c.who != null && (emap.get(c.who.toLowerCase()) ?? c.who) === by)
  }
  return ownerLens(assignments, lens.key, await loadRegistry(env))
}

/** An owner pool's ledger fold for a scan (null: no pool, or no assignments). */
async function poolLensFor(env: Env, date: string, owner: OwnerScope | undefined): Promise<FoldedLens | null> {
  if (!owner || !(await hasLedger(env))) return null
  return poolLens(await ownerAssignments(env, date), owner, await loadRegistry(env))
}

/** A node in an owner pool once assignments apply: the pool's bytes and
 * objects there (`pl.b` / `pl.o`), exactly its in-pool rows when no assignment
 * moved any (the common case), else shaped like the in-pool rows (stretched by
 * the bands assigned in) or, when none, like the subtree total. An unowned
 * node has no owners to split. */
function poolAgg(pl: FoldedLens, owner: OwnerScope, path: string, all: Agg | null, mine: Agg): Agg {
  if (!all) return mine
  const vb = pl.value(path, all.b, mine.b)
  const vo = Math.round(pl.o.value(path, all.o, mine.o))
  if (Math.abs(vb - mine.b) < 0.5 && vo === mine.o) return mine
  if (vb <= 0 && vo <= 0) return newAgg()
  const shape = mine.b > 0 ? mine : all
  const out = shape.b > 0 ? scale(shape, vb / shape.b) : { ...newAgg(), kind: shape.kind, nc: shape.nc, nd: shape.nd }
  out.b = vb
  out.o = vo
  if (owner === 'unowned') out.ub = {}
  return out
}

/** A node under a user lens once assignments apply: U's bytes there (`ol.value`),
 * shaped like the subtree total when that was read (an assigned band is all
 * of the node's mix), else like U's attributed slice — stretched when U's
 * assignments below add bytes that slice never had. Every byte is U's, so the
 * per-user split is U alone. `mine` null = the node was not read (an unread
 * ancestor of an assigned region): its bytes are its bands below. */
function lensAgg(ol: FoldedLens, user: string, path: string, all: Agg | null, mine: Agg | null): Agg {
  const v = ol.value(path, all?.b ?? null, mine?.b ?? null)
  if (v <= 0) return newAgg()
  const shape = all ?? mine
  const out = shape && shape.b > 0 ? scale(shape, v / shape.b) : newAgg()
  if (!shape || shape.b <= 0) out.b = v
  // Objects from their own fold. It wants the total exactly where the bytes
  // fold does, except under a cover U holds every byte of but not every
  // object of (zero-byte objects attributed to others): unread there, so
  // those keep the byte-proportional count.
  if (all || !ol.o.needsTotal(path) || ol.o.isAssigned(path)) out.o = Math.round(ol.o.value(path, all?.o ?? null, mine?.o ?? null))
  out.ub = { [user]: out.b }
  return out
}

/** Everything a view needs before assembly: the scoped root, the kept
 * (above-threshold, ancestor-closed) scoped aggregates under it, the fold
 * counts, and which tier answered. `null` = nothing in scope under P. */
interface Read {
  rootAll: Agg
  rootAgg: Agg
  kept: Map<string, Agg>
  aggDepth: Map<string, number>
  foldedOf: Map<string, number>
  threshold: number
  thrAt: (depth: number) => number
  tier: string
  idx: IndexHandle
  truncated: boolean
  matches?: string[]
  matched?: { path: string; b: number; o: number }[]
  /** A rollup read: the match count (a bound) and the rollup's shape (`View.rollup`). */
  matchCount?: { n: number; b: number; o: number }
  rollup?: { children: number; kept: number; rows: number | null; bucketsOnly?: true; scopedBelow?: true }
  /** A rollup read: every known node's exact matched total on this scan, drawn or not — a diff takes a name
   * one side didn't draw from here instead of a point lookup, absent = zero under a parent `complete` says
   * is known whole. */
  exact?: Map<string, Agg>
  complete?: (p: string) => boolean
  /** A rollup read: the paths answered (P and the nested ones; `ViewOpts.nestForce`). */
  nested?: Set<string>
  /** With a NOT query: the outermost excluded paths under the match roots. */
  excluded?: string[]
  /** `matches` as a set, when the read holds one (a static literal's, per isolate). */
  matchSet?: Set<string>
  /** …and their scoped aggregates (what a diff's point lookups subtract). */
  excl?: Map<string, Agg>
  firstPaint?: boolean
  /** A filter view's phase 2 left some roots' insides unread (`View.interiors`). */
  interiors?: { read: number; skipped: number; reason: string; late?: number }
  /** The assignments fold behind a user lens (null: no lens, or no assignments). */
  ownerLens: FoldedLens | null
  /** A path's scoped share of its total (owner pool / lens applied). `all` =
   * the subtree total (null under a lens where the path's cover isn't U's);
   * `mine` = the rows the sort's lens/pool kept (null = unread, lens only). */
  scoped: (p: string, all: Agg | null, mine: Agg | null) => Agg
}

/** `cov` collects why a filtered read may be missing matches — also when it
 * found none (`null`). */
async function readView(env: Env, o: ViewOpts, cov: Coverage = {}): Promise<Read | null> {
  const { date, path, w, h, minArea, atten, lens, owner, query, maxDepth, classes } = o
  const dP = path === '' ? 0 : path.split('/').length
  const sort = lens ? await lensSort(env, date) : 'path'
  const readRoot = (idx: IndexHandle) =>
    path === '' ? readRows(idx, 1, 1, '', '￿', undefined, lens) : readRows(idx, dP, dP, path, path, undefined, lens)
  // A user lens applies the ownership ledger: assignments repaint attribution, so
  // U's bytes under a path can include other people's slices (a band U
  // assigned) or lose U's own (a band someone else assigned). Where the former
  // can happen the by-path tier is read too, so every node's total is known.
  const ol = lens ? await ownerLensFor(env, date, lens, o.by) : null
  // Where the by-path tier is read for a lens: P itself when U's assignment
  // covers it, else the largest REGION_READS of U's assigned regions under P,
  // to draw inside them. The rest are nodes valued exactly from the manifest
  // (an assignment's total is known without a read) — leaf tiles until drilled,
  // like a fold; a user with hundreds of scattered assignments would otherwise
  // touch more row groups at the root than one view may.
  const rootTotal = !!ol && ol.needsTotal(path)
  const allRegions = ol && !rootTotal ? ol.regions(path) : []
  const regions: { path: string; depth: number }[] = rootTotal
    ? [{ path, depth: dP }]
    : [...allRegions].sort((a, b) => b.all - a.all).slice(0, REGION_READS)
  // Owner pools filter rows (they are owner slices); a user lens folds the
  // assignments on top of U's rows (`lensAgg`).
  // A pool folds the ledger the same way: assignments since the scan move bytes
  // in or out of it (`poolAgg`); every slice is read, so `all` is known.
  const pl = lens ? null : await poolLensFor(env, date, owner)
  const scoped = (p: string, all: Agg | null, mine: Agg | null): Agg =>
    ol ? lensAgg(ol, lens!.key, p, all, mine) : pl ? poolAgg(pl, owner!, p, all, mine!) : mine!

  // Root aggregate P.b (and P's own us for the response root) from the
  // coarsest tier that has P at all — the same numbers in every tier. The
  // by-path tier of the same name serves the lens's region reads.
  const tr = o.trace
  let t0 = performance.now()
  const tiers = (await Promise.all(COARSE_EXPS.map(async e => {
    const [idx, allIdx] = await Promise.all([
      tryOpen(env, date, `coarse${e}${sort === 'path' ? '' : `-${sort}`}`),
      regions.length ? tryOpen(env, date, `coarse${e}`) : null,
    ])
    if (!idx || floorOf(idx) == null) return null
    return { name: `coarse${e}`, idx: withTrace(idx, tr), ...(allIdx ? { all: withTrace(allIdx, tr) } : {}) }
  }))).filter((t): t is { name: string; idx: IndexHandle; all?: IndexHandle } => t != null)
  tr?.('open', performance.now() - t0)
  let rootRows: Row[] = []
  let fine: IndexHandle | null = null
  t0 = performance.now()
  for (const t of tiers) {
    rootRows = await readRoot(t.idx)
    if (rootRows.length) break
  }
  if (!rootRows.length) {
    fine = withTrace(await openFine(env, date, sort, lens), tr)
    rootRows = await readRoot(fine)
    // A lens user the scan attributes nothing to under P may still own it
    // all by assignment: an empty attribution root is a zero, not a miss.
    if (!rootRows.length && !ol) throw new NotFound(path)
  }
  const rootAll = newAgg()
  const rootMine = newAgg()
  for (const r0 of rootRows) {
    const r = classRow(r0, classes)
    merge(rootAll, r)
    if (ownerOk(r.usr, owner)) merge(rootMine, r)
  }
  // The fleet root has no row of its own: its children are the depth-1 paths.
  if (path === '') rootAll.nc = rootMine.nc = new Set(rootRows.map(r => r.path)).size
  const nDesc = subtreeRows(path, rootRows)
  const smallRows = o.smallRows ?? SMALL_SUBTREE_ROWS
  const ivPlanRows = o.ivPlanRows ?? IV_PATH_PLAN_ROWS
  // P's total, when U's assignment covers P (one point read on the by-path tier).
  let rootTot: Agg | null = null
  if (rootTotal) {
    rootTot = newAgg()
    for (const r of await readRootAllRows(env, date, path, dP)) merge(rootTot, r)
  }
  let rootAgg = scoped(path, ol ? rootTot : rootAll, rootMine)
  // An assignments-only root (no attributed row) has bytes from its bands but no
  // object count of its own: take the regions' manifest counts.
  if (ol && rootAgg.b > 0 && rootAgg.o === 0) rootAgg.o = allRegions.reduce((n, r) => n + r.objects, 0)
  // Nothing in scope under P: an empty view, not a zero threshold that would
  // keep every row the tier holds.
  if (rootAgg.b <= 0) return null
  const threshold = o.threshold ?? (rootAgg.b * minArea) / (w * h)
  const thrAt = (depth: number) => threshold * atten ** Math.max(0, depth - dP - 1)

  // The coarsest tier whose floor the UNSCOPED query can't see below. A
  // narrow scope lowers the node threshold (its root is smaller), but the
  // tier is chosen by the view's total bytes: the same rows a plain view of
  // P reads, scoped per node. Paths below that tier's floor fold into their
  // parent's (other) — exactly, since (other) = parent − Σ kept kids on the
  // scoped aggregates — instead of dragging the read onto the floor-free
  // tier for every scoped root view. Under a lens the view's total is U's
  // bytes once assignments apply — it can exceed U's attributed root (`rootAll`)
  // many times over for an assignments-heavy user — so plan by the larger: assignments
  // are read from the same tier's by-path file, whose floor bounds an assigned
  // node's total the way the by-user floor bounds an attributed slice, and
  // a root that dropped to the floor-free tier would read every region there.
  const lensRoot = ol ? lensAgg(ol, lens!.key, path, rootTot, rootMine).b : 0
  const thrAll = o.threshold ?? (Math.max(rootAll.b, lensRoot) * minArea) / (w * h)
  let pick: { name: string; idx: IndexHandle; all?: IndexHandle } | null = null
  for (const t of tiers) {
    if (thrAll >= floorOf(t.idx)!) { pick = t; break }
  }
  tr?.('root', performance.now() - t0)
  if (!pick) pick = { name: 'fine', idx: fine ?? withTrace(await openFine(env, date, sort, lens), tr), ...(regions.length ? { all: withTrace(await openFine(env, date, 'path'), tr) } : {}) }
  const idx = pick.idx
  const pLo = path === '' ? '' : path + '/'
  const pHi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'

  // --- the filter view (specs/filter-views.md §2): a match selects its
  // subtree, found adaptively. Phase 1 resolves the match roots on the
  // coarsest tier independent of the pixel budget (every path over its
  // absolute floor, one small read; deeper tiers only if it found nothing,
  // at the plain view's threshold so the read stays bounded). Phase 2 reads
  // each root's subtree as a region at one forest threshold (the budget split
  // by bytes), attenuated from the root's own depth. Under a user lens the
  // assignments fold owns the read; the post-read filter below stays for that.
  // NOT (`-x`): a match root's totals exclude every descendant path the
  // negative part holds — its outermost such descendants' totals are
  // subtracted, and they drop out of the drawn tree (the `(other)` fold is
  // then P − Σ kept on the net values). A view root the query matches is
  // its own single match root when there are negatives to exclude.
  const negP = query?.neg ?? null
  const rootHit = !!query && query(path)
  if (query && !ol && (!rootHit || negP)) {
    const sumAgg = (into: Agg, from: Agg) => {
      into.b += from.b; into.o += from.o; into.wts += from.wts; into.wb += from.wb
      if (from.a != null) into.a = into.a == null ? from.a : Math.max(into.a, from.a)
      for (const k in from.cb) into.cb[k] = (into.cb[k] ?? 0) + from.cb[k]
      for (const k in from.ub) into.ub[k] = (into.ub[k] ?? 0) + from.ub[k]
    }
    // `a` less `cut`, keeping what the row says the path is.
    const minus = (a: Agg, cut: Agg | undefined, lostKids = 0): Agg => {
      if (!cut && !lostKids) return a
      const out = cut ? subtract(a, [cut]) : { ...a }
      out.kind = a.kind
      out.nd = a.nd
      out.nc = a.nc == null ? null : Math.max(0, a.nc - lostKids)
      return out
    }
    // Scoped aggregates per path from a row set (class + owner pool). Without an owner pool every row is
    // in scope, so a path's scoped aggregate is its total: one object serves both (a static literal's
    // ~80K roots built two each).
    const aggregate = (rs: Row[]): { all: Map<string, Agg>; mine: Map<string, Agg>; depth: Map<string, number> } => {
      const mine = new Map<string, Agg>(); const depth = new Map<string, number>()
      const all = owner ? new Map<string, Agg>() : mine
      for (const r0 of rs) {
        const r = classRow(r0, classes)
        let m = mine.get(r.path)
        if (!m) { mine.set(r.path, (m = newAgg())); if (owner) all.set(r.path, newAgg()); depth.set(r.path, r.depth) }
        if (ownerOk(r.usr, owner)) merge(m, r)
        if (owner) merge(all.get(r.path)!, r)
      }
      return { all, mine, depth }
    }
    // `aggregate` over the static hits live on `date` (`vf ≤ D < vt`; `usr` '' = unowned), without a row
    // object per hit: a heavy literal's ~80K roots are held per isolate already, and copies cost memory.
    const aggregateHits = (hits: Hit[]): ReturnType<typeof aggregate> => {
      const D = scanAt(date)
      const mine = new Map<string, Agg>(); const depth = new Map<string, number>()
      const all = owner ? new Map<string, Agg>() : mine
      const add = (a: Agg, size: number, n: number, usr: string | null) => { a.b += size; a.o += n; if (usr) a.ub[usr] = (a.ub[usr] ?? 0) + size }
      for (const h of hits) {
        if (!(h.vf <= D && D < h.vt)) continue
        const usr = h.usr === '' ? null : h.usr, size = Number(h.size), n = Number(h.n)
        let m = mine.get(h.path)
        if (!m) { mine.set(h.path, (m = newAgg())); if (owner) all.set(h.path, newAgg()); depth.set(h.path, h.depth) }
        if (ownerOk(usr, owner)) add(m, size, n, usr)
        if (owner) add(all.get(h.path)!, size, n, usr)
      }
      return { all, mine, depth }
    }
    // Phase 1. A store generation with the search sidecars answers from
    // them (specs/path-store-search.md): the match roots anywhere under P,
    // exact unless a budget cut the search; anything else reads as before.
    t0 = performance.now()
    let roots: string[] = []
    let p1: ReturnType<typeof aggregate> | null = null
    let p1Tier = ''
    let searchCut = false
    /** Phase 1 came from the static index: its aggregates know bytes, objects and owners only. */
    let staticRoots = false
    /** …held per isolate (`staticPhase1`). */
    let memo: Phase1Memo | null = null
    // The search reads the `path` sort (its names' row groups are that
    // sort's); a lens without assignments keeps the old read.
    const pathIdx = !tiers.length && !lens ? (sort === 'path' && fine ? fine : withTrace(await openFine(env, date, 'path'), tr)) : null
    const store = !!pathIdx && isStore(pathIdx)
    // The search sidecars sit beside the scan's own path sort: an interval-store handle searches there
    // (a deployment that builds none never opens that per-scan sort for them).
    const searchIdx = pathIdx && pathIdx.asOf != null && env.SEARCH_SIDECARS ? withTrace(await openFine(perScan(env), date, 'path'), tr) : pathIdx
    const pq = query.ast
    const traceSearch = (what: string, f: SearchFound) => tr?.('search', performance.now() - t0, `${what} ${f.stats.mode} c${f.stats.candidates} ${f.stats.layout === 2 ? `r${f.stats.rowsRgs}` : `n${f.stats.namesRgs} g${f.stats.pathRgs}`}${f.truncated ? ' cut' : ''}`)
    if (rootHit) {
      // The view root is the match: its aggregate is the root read's.
      roots = [path]
      p1 = { all: new Map([[path, rootAll]]), mine: new Map([[path, rootMine]]), depth: new Map([[path, dP]]) }
      p1Tier = 'root'
    } else {
      // The static name index (`staticFilter.ts`): a single literal's match roots on any scan of its
      // generation, exact, from one cached suffix-range read — no search sidecars, no thresholded walk.
      const sfs = !lens && !classes ? staticFilterStore(env) : null
      const skey = sfs ? await staticKey(sfs, pq, [date]) : null
      // A literal the static index answers, on a scan its generation doesn't hold (newer than its newest — published,
      // not yet appended — or before its base): `scan-not-indexed` on every deployment, indexed-only or not — never the
      // thresholded walk, which can't see small matches and reads as "no matches". Queries the index can't answer at
      // all (regexes, globs, several terms: no literal) still walk.
      if (sfs && !skey && staticLiteral(pq)) throw new FilterRejected(reject('scan-not-indexed'))
      // A dir-only (v1) scan's index never saw a file: whatever answers, its matches are its folders'.
      if (skey && (await dirsOnlyOf(sfs, [date])).length) noteCoverage(cov, 'dirsOnly', DIRS_ONLY)
      const raw = skey ? await sfs!.source.hits(skey, path, { firstPaint: o.firstPaint }) : null
      let shits = raw
      // An answer that doesn't cover the scan — a heavy literal past the drill base (a run with no live `drill/` cuts
      // the drill's stack: that run's scans and every later one's), a light read over a stack cut mid-read, the
      // catalog's buckets on a scan past its stack — or a heavy literal its heavy source declined: `scan-not-indexed`,
      // never the thresholded walk, which can't see the literal's small matches and reads as "no matches".
      if (skey && (raw ? !covers(raw, [date]) : !!sfs!.source.heavyDeclined?.(skey))) throw new FilterRejected(reject('scan-not-indexed'))
      // A heavy literal's rollups know no owners: under one, the view reads as before.
      const off = shits?.rollup && owner ? 'rollup: no owners' : null
      if (off) shits = null
      // An indexed-only deployment never walks the path store for a filter (`indexedOnly.ts`); nor does a heavy
      // literal with no drilldown (`FILTER_STATIC_HEAVY` off), whose thresholded walk would read as "no
      // matches": below the fleet root it is `term-too-common`, and the root's catalog buckets know no owners.
      // Nor does an anchored term on a generation with no anchors build (`anchor-not-indexed`), indexed-only or not.
      const noDrill = !!sfs && !!skey && sfs.source.heavy === false
      const why = shits ? null : (noDrill || raw?.rollup?.scopedBelow) && raw?.rollup?.bucketsOnly && owner ? reject('unsupported-scope') : declined(sfs, skey, raw)
      if (why && (indexedOnly(env) || why.code === 'anchor-not-indexed' || (noDrill && why.code !== 'scan-not-indexed'))) throw new FilterRejected(why)
      tr?.('static', performance.now() - t0, shits ? `${skey} ${shits.rollup ? `rollup ${shits.rollup.cells.length}` : shits.hits.length}` : skey ? `declined${off ? ` (${off})` : ''}` : undefined)
      if (shits?.rollup) {
        const h = pathIdx ?? fine ?? withTrace(await openFine(env, date, 'path'), tr)
        return rollupRead(env, o, shits.rollup, { dP, rootAll, idx, details: h, trace: tr, fetch: (p, ho) => sfs!.source.hits(skey!, p, ho) })
      }
      if (shits) {
        // Per isolate: the same held hits, scan and owner scope aggregate the same (a view's first paint,
        // its full read, a diff's sides and floors, other canvases).
        const memoKey = `${path}\0${date}\0${owner ? ownerKey(owner) : ''}`
        let byHits = staticPhase1.get(shits.hits!)
        if (!byHits) staticPhase1.set(shits.hits!, (byHits = new Map()))
        let m = byHits.get(memoKey)
        if (!m) {
          const agg = aggregateHits(shits.hits!)
          m = { p1: agg, roots: [...agg.depth.keys()].sort() }
          byHits.set(memoKey, m)
          if (byHits.size > 8) byHits.delete(byHits.keys().next().value!)
        }
        p1 = m.p1
        roots = m.roots
        memo = m
        p1Tier = shits.io.from === 'drill' ? 'drill' : 'static'
        staticRoots = true
      }
      const plan = !shits && store && pq ? planPositive(pq) : null
      const searched = plan ? await searchRoots(perScan(env), searchIdx!, query, plan, path, o.searchLimits, `pos:${JSON.stringify(pq)}`) : null
      if (searched) traceSearch('pos', searched)
      // A search cut before it found anything (its heaviest name alone is
      // over budget) says nothing: the thresholded read below answers instead.
      const found = searched && (searched.roots.length || !searched.truncated) ? searched : null
      if (found) {
        p1 = aggregate(found.rows)
        roots = found.roots
        p1Tier = 'search'
        searchCut = found.truncated
        noteCoverage(cov, 'partial', found.reason)
      } else if (searched) {
        noteCoverage(cov, 'partial', `the search stopped before finding a match (${searched.reason}); showing a thresholded read`)
      } else if (!shits) {
        // Phase 1 is a thresholded read: what it can't see, it can't match.
        noteCoverage(cov, 'approximate', lens ? APPROX_LENS : !store || plan ? APPROX_NO_INDEX : APPROX_UNINDEXED)
      }
      for (const t of found || shits ? [] : tiers) {
        const rs = await readRows(t.idx, dP + 1, 1e9, pLo, pHi, t === tiers[0] ? undefined : thrAt)
        p1 = aggregate(rs)
        roots = matchRoots(p1.depth.keys(), query, path)
        p1Tier = t.name
        if (roots.length) break
      }
      const fineIdx = roots.length || found || shits ? null : pathIdx ?? fine ?? withTrace(await openFine(env, date, 'path'), tr)
      if (fineIdx && !isStore(fineIdx) && rootAll.o > (o.v1ScanObjects ?? V1_FILTER_SCAN_OBJECTS)) noteCoverage(cov, 'approximate', APPROX_V1_TOO_BIG)
      if (fineIdx && (isStore(fineIdx) || rootAll.o <= (o.v1ScanObjects ?? V1_FILTER_SCAN_OBJECTS))) {
        // Every depth: `maxDepth` caps what phase 2 draws, never where
        // matches are found (a `depth=1` first paint must still find deep ones).
        const got = await readSubtree(env, date, fineIdx, [{ dLo: dP + 1, dHi: 1e9, pLo, pHi }], thrAt, undefined, nDesc, smallRows, tr, ivPlanRows)
        p1 = aggregate(got.rows)
        roots = matchRoots(p1.depth.keys(), query, path)
        p1Tier = isStore(fineIdx) ? got.variant : 'fine'
      }
    }
    tr?.('match', performance.now() - t0)
    if (!roots.length) return null
    // Unscoped, nothing to exclude: the forest's budget-free part is the isolate's (`Phase1Memo`).
    const lean = memo && !owner && !negP && !pl ? (memo.forest ??= buildForest(memo, path, dP), memo) : null
    const rootSet = lean ? (lean.rootSet ??= new Set(roots)) : new Set(roots)
    const depthF: Map<string, number> = lean ? new Overlay(lean.forest!.depth) : new Map<string, number>(roots.map(r => [r, p1!.depth.get(r)!]))
    // The match root a path is strictly under (null: none; `''` is the store
    // root, a real root — test against null, never truthiness).
    const rootFor = (p: string): string | null => {
      if (p === path) return null
      for (let q = parentOf(p); q.length >= path.length; q = parentOf(q)) { if (rootSet.has(q)) return q; if (q === '') return null }
      return null
    }
    const rootAggOf: Map<string, Agg> = lean ? new Overlay(lean.p1.mine) : new Map<string, Agg>(roots.map(r => [r, scoped(r, p1!.all.get(r)!, p1!.mine.get(r)!)]))
    // Excluded paths (NOT): the outermost paths under a match root the
    // negative part holds — from the index, else from the rows phase 1 read.
    const excl = new Map<string, Agg>()
    /** Σ excluded aggregates under each path, and excluded direct children. */
    const cut = new Map<string, Agg>()
    const lostKids = new Map<string, number>()
    /** Memoized `netRoot`s; an exclusion clears them. */
    const netOf = new Map<string, Agg>()
    const exclude = (e: string, a: Agg) => {
      netOf.clear()
      excl.set(e, a)
      const r = rootFor(e)!
      lostKids.set(parentOf(e), (lostKids.get(parentOf(e)) ?? 0) + 1)
      for (let q = parentOf(e); ; q = parentOf(q)) {
        let c = cut.get(q)
        if (!c) cut.set(q, (c = newAgg()))
        sumAgg(c, a)
        if (q === r) break
      }
    }
    const underExcl = (p: string): boolean => {
      for (let q = p; q.length > path.length; q = parentOf(q)) { if (excl.has(q)) return true; if (q === '') break }
      return false
    }
    if (negP && pq) {
      t0 = performance.now()
      const nplan = store ? planNegative(pq) : null
      const ns = nplan ? await searchRoots(perScan(env), searchIdx!, negP, nplan, path, o.searchLimits, `neg:${JSON.stringify(pq)}`) : null
      if (ns) {
        traceSearch('neg', ns)
        if (ns.truncated) searchCut = true
        noteCoverage(cov, 'partial', ns.reason && `exclusions: ${ns.reason}`)
        const an = aggregate(ns.rows)
        for (const e of ns.roots) if (rootFor(e) !== null) exclude(e, scoped(e, an.all.get(e)!, an.mine.get(e)!))
      } else if (p1) {
        // (Already approximate for the same cause: one note says it.)
        if (!cov.approximate) noteCoverage(cov, 'approximate', store && !nplan ? APPROX_EXCL_UNINDEXED : APPROX_EXCL_NO_INDEX)
        for (const e of matchRoots(p1.depth.keys(), negP, path)) if (rootFor(e) !== null) exclude(e, scoped(e, p1.all.get(e)!, p1.mine.get(e)!))
      }
    }
    // A root's net aggregate, memoized in `netOf` (sorts and filters ask it per comparison).
    const netRoot = (r: string): Agg => {
      if (lean) return rootAggOf.get(r)!
      let a = netOf.get(r)
      if (!a) netOf.set(r, (a = minus(rootAggOf.get(r)!, cut.get(r), lostKids.get(r))))
      return a
    }
    const matchedPre = lean ? { ...lean.forest!.total } : newAgg()
    if (!lean) for (const r of roots) sumAgg(matchedPre, netRoot(r))
    if (matchedPre.b <= 0) return null
    // Phase 2.
    const T = o.threshold ?? filterThreshold(matchedPre.b, w, h, minArea)
    // A diff's floor pass wants this side's floor alone: no forest.
    if (o.floorOnly) return { rootAll, rootAgg: matchedPre, kept: new Map(), aggDepth: new Map(), foldedOf: new Map(), threshold: T, thrAt: rebasedThreshold(T, atten, dP), tier: p1Tier, idx, truncated: false, ownerLens: ol, scoped }
    const withFloors = tiers.filter(t => floorOf(t.idx) != null).map(t => ({ name: t.name, floor: floorOf(t.idx)!, idx: t.idx }))
    const chosen = o.firstPaint ? (withFloors[0] ?? 'fine') : pickTier(withFloors, T)
    const regionIdx = chosen === 'fine' ? (fine ?? withTrace(await openFine(env, date, 'path'), tr)) : chosen.idx
    let tierName = chosen === 'fine' ? 'fine' : chosen.name
    // A root under its level's threshold folds into its parent's `(other)` (below): nothing inside it
    // is drawn, so it is neither read nor looked up.
    const drawn = (r: string) => netRoot(r).b >= T * atten ** Math.max(0, depthF.get(r)! - dP - 1)
    // Phase 2 subdivides only the roots the canvas can show structure in: drawn, holding more than one
    // object (a single-object root is a leaf, and its rect would still cost a span plan — on the size
    // sort a group per size bucket: `tomat` under `podcast_audio`, 24 single-file roots, ~25 s), and at
    // least `FILTER_SUBDIV_AREA` px² (bytes ≥ T · that / minArea). A smaller root is one tile, exact from
    // phase 1 (its bytes, objects and owners), with nothing inside it read; a drill re-reads it whole.
    const subdivMin = T * (FILTER_SUBDIV_AREA / minArea)
    // `maxDepth` caps the forest at dP + N (a `depth=1` diff walks one level): a root at or below the cap
    // has nothing inside it drawn, nor is its own row looked up past it.
    const shown = (r: string) => maxDepth == null || depthF.get(r)! <= dP + maxDepth
    // The `k` heaviest of `rs` by net bytes, ties by path (decorated once: a comparator asking `netRoot` cost
    // ~2.6M map reads to order a static literal's ~80K roots).
    const heaviest = (rs: string[], k: number): string[] => rs.map(r => [netRoot(r).b, r] as const)
      .sort((x, y) => y[0] - x[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0)).slice(0, k).map(x => x[1])
    const readRoots = heaviest(roots.filter(r => drawn(r) && netRoot(r).o > 1 && netRoot(r).b >= subdivMin && (maxDepth == null || depthF.get(r)! < dP + maxDepth)), REGION_READS)
      .map(r => ({ path: r, depth: depthF.get(r)! }))
    const loose = looseThreshold(T, atten, readRoots.map(r => r.depth))
    t0 = performance.now()
    // Static roots carry bytes, objects and owners only: each root's own rows (kind, written time,
    // read day, class mix, child counts) come from the `path` sort by point lookups, the heaviest
    // `ROOT_DETAILS` roots, beside phase 2 (one batched read; a batch too wide leaves them plain).
    // The first paint skips both: the roots alone, exact, from the static read.
    const firstPaintStatic = staticRoots && !!o.firstPaint
    let detailsOff = false
    const details = staticRoots && !firstPaintStatic && !(maxDepth != null && maxDepth <= 0) ? (async () => {
      const want = heaviest(roots.filter(r => drawn(r) && shown(r)), ROOT_DETAILS)
      if (!want.length) return []
      const asks = new Set(want.map(r => `${depthF.get(r)}\0${r}`))
      try {
        const h = pathIdx ?? fine ?? withTrace(await openFine(env, date, 'path'), tr)
        return (await readAsks(h, want.map(r => ({ depth: depthF.get(r)!, path: r })), r => asks.has(`${r.depth}\0${r.path}`), { maxGroups: DETAIL_GROUPS, stop: () => detailsOff, rememberWide: true })).rows
      } catch (e) {
        if (!/too wide/.test(String((e as Error).message ?? e))) throw e
        tr?.('details', 0, 'too wide')
        return []
      }
    })() : Promise.resolve([] as Row[])
    let rows2: Row[] = []
    let variant: string | undefined
    let interiors: Read['interiors']
    if (!(maxDepth != null && maxDepth <= 0) && !firstPaintStatic && readRoots.length && o.phase2Gate?.dead) {
      // An earlier round of this request read nothing in its time: this one would too.
      interiors = { read: 0, skipped: readRoots.length, reason: `${readRoots.length} past the time budget`, late: readRoots.length }
      tr?.('interiors', 0, `gated ${readRoots.length}`)
    } else if (!(maxDepth != null && maxDepth <= 0) && !firstPaintStatic && readRoots.length) {
      // One read per root depth, each at that depth's own threshold (rows are re-tested per root
      // below, so the kept set is the one-read answer's): one read at the deepest root's threshold
      // took every shallower root's subtree at a fraction of its own — `tomat`'s four depth-2 dirs at
      // half their threshold for one depth-3 root. In parallel: run in turn, roots spread over many
      // depths (`00241`) paid each depth's span plans and fetches in series.
      const byDepth = new Map<number, { path: string; depth: number }[]>()
      for (const r of readRoots) byDepth.set(r.depth, [...(byDepth.get(r.depth) ?? []), r])
      const weight = (rs: { path: string }[]) => rs.reduce((n, r) => n + netRoot(r.path).b, 0)
      const groups = [...byDepth.values()].sort((x, y) => weight(y) - weight(x))
      // Bounded: each root's insides to `FILTER_SUBDIV_LEVELS` levels (deeper rows come back childless,
      // drillable — and every level costs the `path` sort a group per root), the reads planned first and
      // admitted heaviest first within `FILTER_PHASE2_GROUPS` row groups (each a ranged GET of up to
      // 32K rows from the store, six in flight per request: `00241`'s 167 groups took 15 s), and the
      // whole phase within `FILTER_PHASE2_MS`. A root left out is drawn as one exact tile; the response
      // says how many (`interiors`).
      const ms = o.phase2Ms ?? (Number(env.FILTER_PHASE2_MS) || FILTER_PHASE2_MS)
      const late = Symbol('late')
      let timer: ReturnType<typeof setTimeout> | null = null
      // Past the budget the abandoned reads stop decoding (`stop`): they'd hold the isolate's CPU, and the
      // answer is built on that same thread.
      let over = false
      const stop = () => over
      const expired = new Promise<typeof late>(r => { timer = setTimeout(() => { over = true; r(late) }, ms) })
      const skipped = { budget: 0, wide: 0, late: 0 }
      const settle = async <X>(p: Promise<X>): Promise<X | typeof late | Error> => {
        p.catch(() => {})
        try { return await Promise.race([p, expired]) } catch (e) {
          if (e instanceof TooWide || /too wide/.test(String((e as Error).message ?? e))) return e as Error
          throw e
        }
      }
      const capLevels = (q: Rect, r: { path: string }) => r.path === path ? q : { ...q, dHi: Math.min(q.dHi, q.dLo + FILTER_SUBDIV_LEVELS - 1) }
      const planFor = (rs: { path: string; depth: number }[]) => {
        const nd = rootHit ? nDesc : rs.reduce<number | null>((n, r) => { const d = p1!.all.get(r.path)?.nd; return n == null || d == null ? null : n + d }, 0)
        const rects = rootRects(rs).map((q, i) => capLevels(q, rs[i])).map(q => maxDepth != null ? { ...q, dHi: Math.min(q.dHi, dP + maxDepth) } : q)
        // Level-capped rects read the `path` sort alone: ~a group per level per root, so its worst case (a
        // deep subtree's every level) can't happen, and planning `bysize` too doubled the span queries —
        // D1 runs them in turn, and a diff's two sides' plans took seconds (`00241`: 15–17 s of spans).
        const capped = rs.every(r => r.path !== path)
        return settle(planSubtree(env, date, regionIdx, rects, rebasedThreshold(T, atten, rs[0].depth), undefined, nd, smallRows, tr, capped, ivPlanRows))
      }
      const plans = await Promise.all(groups.map(planFor))
      let room = o.phase2Groups ?? FILTER_PHASE2_GROUPS
      let roomRows = o.phase2Rows ?? FILTER_PHASE2_ROWS
      const admitted: { rs: { path: string }[]; read: Promise<Row[] | typeof late | Error>; variant: string }[] = []
      const admit = (rs: { path: string }[], pl: SubtreePlan | typeof late | Error): boolean => {
        if (pl === late) skipped.late += rs.length
        else if (pl instanceof Error) skipped.wide += rs.length
        else if (pl.groups > room || pl.rows > roomRows) return false
        else { room -= pl.groups; roomRows -= pl.rows; admitted.push({ rs, read: settle(pl.read(stop)), variant: pl.variant }) }
        return true
      }
      // A depth's roots together over the budget: its heaviest `FILTER_SPLIT_ROOTS` planned one by one, each
      // admitted if it fits (the lighter ones' rects can be most of a shared read).
      const wide: { path: string; depth: number }[][] = []
      plans.forEach((pl, i) => { if (!admit(groups[i], pl)) wide.push(groups[i]) })
      for (const rs of wide) {
        // Past the time budget no solo plan can land: none is started (each costs D1 span queries).
        if (over) { skipped.late += rs.length; continue }
        const each = rs.slice(0, rs.length > 1 ? FILTER_SPLIT_ROOTS : 0)
        const solo = await Promise.all(each.map(r => planFor([r])))
        each.forEach((r, i) => { if (!admit([r], solo[i])) skipped.budget++ })
        skipped.budget += rs.length - each.length
      }
      for (const x of admitted) {
        const got = await x.read
        if (got === late) skipped.late += x.rs.length
        else if (got instanceof Error) skipped.wide += x.rs.length
        else { rows2.push(...got); variant ??= x.variant }
      }
      if (timer != null) clearTimeout(timer)
      if (o.phase2Gate && skipped.late && !rows2.length) o.phase2Gate.dead = true
      const left = skipped.budget + skipped.wide + skipped.late
      if (left) {
        const why = [skipped.budget && `${skipped.budget} over the read budget`, skipped.wide && `${skipped.wide} too wide`, skipped.late && `${skipped.late} past the time budget`].filter(Boolean).join(', ')
        interiors = { read: readRoots.length - left, skipped: left, reason: why, ...(skipped.late ? { late: skipped.late } : {}) }
        tr?.('interiors', 0, `skipped ${left}`)
      }
      if (isStore(regionIdx) && variant) tierName = variant
    }
    tr?.('rows', performance.now() - t0, variant)
    // The lookups ride beside phase 2 and may wait `FILTER_DETAILS_MS` past it, no longer: they are
    // display fields (kind, ages, classes) and their span plans cost seconds where roots spread over
    // many depths (`00241`: ~8.5 s for 48 roots, against 6 s for phase 2).
    const budget = o.detailsWait ?? (Number(env.FILTER_DETAILS_MS) || Infinity)
    const late = Symbol('late')
    const detailRows = budget === Infinity ? await details
      : await Promise.race([details, new Promise<typeof late>(r => setTimeout(() => r(late), budget))]).then(x => {
        if (x !== late) return x
        tr?.('details', performance.now() - t0, 'late')
        detailsOff = true
        details.catch(() => {})
        return [] as Row[]
      })
    if (detailRows.length) {
      const d = aggregate(detailRows)
      for (const [r, m] of d.mine) {
        const a = rootAggOf.get(r)
        if (!a) continue
        // A copy: phase 1's aggregates may be the isolate's (`staticPhase1`), shared across views.
        rootAggOf.set(r, { ...a, kind: m.kind, wts: m.wts, wb: m.wb, a: m.a, cb: m.cb, nc: m.nc, nd: m.nd })
      }
      tr?.('details', performance.now() - t0, String(d.mine.size))
      netOf.clear()
    }
    const p2 = aggregate(rows2)
    // Excluded paths only phase 2 saw (without the index, below phase 1's read).
    if (negP) {
      const fresh = matchRoots([...p2.depth.keys()].filter(p => rootFor(p) !== null && !underExcl(p)), negP, path)
      for (const e of fresh) exclude(e, scoped(e, p2.all.get(e)!, p2.mine.get(e)!))
    }
    const thrTop = (d: number) => T * atten ** Math.max(0, d - dP - 1)
    const forest = lean ? leanForest() : fullForest()
    if (!forest) return null
    const { matchedAgg, rootNet, aggsF, foldedF } = forest
    // A view's forest from the isolate's (`Phase1Memo`): the drawn nodes' aggregates as held — the ancestors'
    // copied before a looked-up root's ages and classes are added to them — and the folds counted.
    function leanForest(): { matchedAgg: Agg; rootNet: Map<string, Agg>; aggsF: Map<string, Agg>; foldedF: Map<string, number> } {
      const F = lean!.forest!
      const matchedAgg = { ...F.total, cb: { ...F.total.cb }, ub: { ...F.total.ub } }
      const aggsF = new Map<string, Agg>()
      for (const [p, b] of F.nb) if (b >= thrTop(F.depth.get(p)!)) aggsF.set(p, rootSet.has(p) ? rootAggOf.get(p)! : F.sums.get(p)!)
      const foldedF = new Map<string, number>()
      for (const p of F.nb.keys()) {
        if (aggsF.has(p)) continue
        const par = parentOf(p)
        if (par === path || aggsF.has(par)) foldedF.set(par, (foldedF.get(par) ?? 0) + 1)
      }
      // The looked-up roots' own rows (ages, classes): into the total and their drawn ancestors, as summing
      // the roots would have.
      const copied = new Set<string>()
      const addDetail = (into: Agg, d: Agg) => {
        into.wts += d.wts; into.wb += d.wb
        if (d.a != null) into.a = into.a == null ? d.a : Math.max(into.a, d.a)
        for (const k in d.cb) into.cb[k] = (into.cb[k] ?? 0) + d.cb[k]
      }
      for (const [r, d] of rootAggOf as Overlay<string, Agg>) {
        addDetail(matchedAgg, d)
        for (let q = parentOf(r); q.length > path.length; q = parentOf(q)) {
          const a = aggsF.get(q)
          if (a) {
            if (!copied.has(q)) { copied.add(q); aggsF.set(q, { ...a, cb: { ...a.cb }, ub: { ...a.ub } }) }
            addDetail(aggsF.get(q)!, d)
          }
          if (q === '') break
        }
      }
      return { matchedAgg, rootNet: rootAggOf, aggsF, foldedF }
    }
    function fullForest(): { matchedAgg: Agg; rootNet: Map<string, Agg>; aggsF: Map<string, Agg>; foldedF: Map<string, number> } | null {
      const matchedAgg = newAgg()
      const rootNet = new Map<string, Agg>()
      // The forest: the roots and every ancestor between P and them. Their bytes first, as plain numbers,
      // bottom-up (one add per node); then aggregates for the drawn nodes only — each root summed once into
      // the nearest drawn node at or above it, and drawn nodes into their drawn parents. A static literal's
      // ~80K roots are almost all folded: an aggregate per root and per ancestor, summed level by level and
      // then deleted, was most of the view's CPU on the edge.
      const nb = new Map<string, number>()
      const levels: string[][] = []
      const atLevel = (p: string, d: number) => (levels[d] ??= []).push(p)
      for (const r of roots) {
        const a = netRoot(r)
        rootNet.set(r, a)
        sumAgg(matchedAgg, a)
        if (r === path) continue
        nb.set(r, a.b)
        atLevel(r, depthF.get(r)!)
      }
      if (matchedAgg.b <= 0) return null
      for (const r of roots) {
        if (r === path) continue
        for (let q = parentOf(r), d = depthF.get(r)! - 1; q.length > path.length; q = parentOf(q), d--) {
          if (nb.has(q)) break
          nb.set(q, 0); depthF.set(q, d); atLevel(q, d)
          if (q === '') break
        }
      }
      for (let d = levels.length - 1; d > dP + 1; d--) {
        for (const p of levels[d] ?? []) { const par = parentOf(p); nb.set(par, nb.get(par)! + nb.get(p)!) }
      }
      // The pixel budget over the forest's own nodes, as the plain view applies it to P's: a match root,
      // or an ancestor holding only roots, under its level's threshold (attenuated from P) folds into its
      // parent's `(other)`. Descendant-inclusive bytes keep the kept set ancestor-closed. Without it a
      // literal with 20K small roots (`tomat`: podcast files) shipped every one as a tile (10 MB).
      const aggsF = new Map<string, Agg>()
      for (const [p, b] of nb) if (b >= thrTop(depthF.get(p)!)) aggsF.set(p, rootNet.get(p) ?? newAgg())
      const foldedF = new Map<string, number>()
      for (const p of nb.keys()) {
        if (aggsF.has(p)) continue
        const par = parentOf(p)
        if (par === path || aggsF.has(par)) foldedF.set(par, (foldedF.get(par) ?? 0) + 1)
      }
      for (const r of roots) {
        if (r === path || aggsF.has(r)) continue
        for (let q = parentOf(r); q.length > path.length; q = parentOf(q)) {
          const k = aggsF.get(q)
          if (k) { sumAgg(k, rootNet.get(r)!); break }
          if (q === '') break
        }
      }
      for (let d = levels.length - 1; d > dP + 1; d--) {
        for (const p of levels[d] ?? []) { const a = aggsF.get(p); if (a) sumAgg(aggsF.get(parentOf(p))!, a) }
      }
      return { matchedAgg, rootNet, aggsF, foldedF }
    }
    const below: string[] = []
    for (const [p, d] of p2.depth) {
      // The view root itself (a query the view matches, e.g. NOT-only) is never in `aggsF`, yet its
      // phase-2 rows are drawn.
      const r = rootFor(p); if (r === null || (r !== path && !aggsF.has(r))) continue
      if (underExcl(p)) continue
      const a = minus(scoped(p, p2.all.get(p)!, p2.mine.get(p)!), cut.get(p), lostKids.get(p))
      if (a.b <= 0) continue
      if (a.b >= rebasedThreshold(T, atten, depthF.get(r)!)(d)) { aggsF.set(p, a); depthF.set(p, d) }
      else below.push(p)
    }
    for (const p of below) {
      const par = parentOf(p)
      if (aggsF.has(par)) foldedF.set(par, (foldedF.get(par) ?? 0) + 1)
    }
    // The tile budget (`tileBudget`): past it only the heaviest nodes are drawn and the rest fold into
    // their parents' `(other)`, exact by subtraction.
    const capped = capTiles(aggsF, depthF, path, o.maxTiles ?? tileBudget(w, h))
    for (const p of capped) {
      const par = parentOf(p)
      if (par === path || aggsF.has(par)) foldedF.set(par, (foldedF.get(par) ?? 0) + 1)
    }
    return {
      rootAll, rootAgg: matchedAgg, kept: aggsF, aggDepth: depthF, foldedOf: foldedF, threshold: T, thrAt: loose,
      tier: `${p1Tier}+${tierName}`, idx: regionIdx, truncated: searchCut || roots.length > HARD_CAP,
      matches: sortedOnce(roots),
      // Heaviest first (ties by path): the list a bulk action and the series read.
      matched: lean ? lean.forest!.matched.slice() : roots.map(r => ({ path: r, b: Math.round(rootNet.get(r)!.b), o: Math.round(rootNet.get(r)!.o) })).sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)),
      matchSet: rootSet,
      ...(excl.size ? { excluded: [...excl.keys()].sort(), excl } : {}),
      ...(o.firstPaint ? { firstPaint: true } : {}), ...(interiors ? { interiors } : {}), ownerLens: ol, scoped,
    }
  }

  // Everything under P that this canvas can draw. Depth is unbounded — the
  // attenuated threshold bounds both row count and depth by construction.
  // `maxDepth` caps the band read at dP + N: the rows at the cap come back
  // childless (the client treats a `c`-less branch as drillable), and the
  // deeper bands — the bulk of the work — are never touched. Not with a
  // query (a lens's assignments fold filters these rows): matches are found at
  // every depth.
  t0 = performance.now()
  const sub = await readSubtree(env, date, idx, [{ dLo: dP + 1, dHi: maxDepth != null && !query ? dP + maxDepth : 1e9, pLo, pHi }], thrAt, lens, nDesc, smallRows, tr, ivPlanRows)
  const rows = sub.rows
  tr?.('rows', performance.now() - t0, sub.variant)
  // A store generation names the sort that answered; a v1 one its tier.
  const tierName = isStore(idx) ? sub.variant : pick.name
  // The lens's assigned regions from the by-path tier (their own rows and
  // everything under them): every path there whose U-share can clear the
  // threshold is present, since all ≥ U's share. P itself as a region means
  // U's assignment covers the whole view: the full range.
  const regionRects: Rect[] = regions.map(r => r.path === path
    ? { dLo: dP + 1, dHi: 1e9, pLo, pHi }
    : { dLo: r.depth, dHi: 1e9, pLo: r.path, pHi: r.path + '0' })
  // On the interval store the regions' totals come from the folded `bysize` sort: one row per path with its
  // exact total, every path over the threshold and no other. (A path-first read over the slices' dyadic
  // segments selects a run per depth per segment: a big user's root regions went past `decodeSpans`' cap.)
  const allRows = !regionRects.length ? []
    : pick.all!.asOf != null ? await readSizeRects(withTrace(await openIndex(folded(env), date, 'bysize'), tr), regionRects, thrAt)
    : await readRects(pick.all!, regionRects, thrAt)
  let aggs = new Map<string, Agg>() // the scoped aggregate per path
  const aggDepth = new Map<string, number>()
  const foldedOf = new Map<string, number>()
  // The unscoped view (the default page, every warm-up target): the scoped
  // aggregate IS the row sum, so the threshold applies to plain per-path
  // byte totals before a single `Agg` exists. The general path below builds
  // two aggregate objects per path for all ~240k rows a root read returns
  // and keeps ~1k of them — 2 s of CPU per view on the edge (Workers Logs,
  // 2026-09-15) against ~0.8 s for the decode itself.
  const plain = !ol && !owner && !classes && !regionRects.length && !query
  if (plain) {
    const tot = new Map<string, number>()
    for (const r of rows) tot.set(r.path, (tot.get(r.path) ?? 0) + r.size)
    for (const [p, b] of tot) {
      const d = p.split('/').length
      if (b >= thrAt(d)) aggDepth.set(p, d)
    }
    for (const r of rows) {
      if (!aggDepth.has(r.path)) continue
      let a = aggs.get(r.path)
      if (!a) aggs.set(r.path, (a = newAgg()))
      merge(a, r)
    }
    // the folded (sub-threshold) child count per kept parent, as below
    for (const [p, b] of tot) {
      if (b <= 0 || aggDepth.has(p)) continue
      const par = parentOf(p)
      const key = aggDepth.has(par) ? par : par === path || (path === '' && !p.includes('/')) ? path : null
      if (key !== null) foldedOf.set(key, (foldedOf.get(key) ?? 0) + 1)
    }
  } else {
  const allAggs = new Map<string, Agg>() // totals per path (the state share's denominator; a lens: only inside its regions)
  const mineAggs = new Map<string, Agg | null>() // the sort's own rows per path (U's slice, or the pool's); null = unread
  for (const r0 of rows) {
    const r = classRow(r0, classes)
    let m = mineAggs.get(r.path)
    if (!m) { mineAggs.set(r.path, (m = newAgg())); aggDepth.set(r.path, r.depth) }
    if (ownerOk(r.usr, owner)) merge(m, r)
    if (!ol) {
      let all = allAggs.get(r.path)
      if (!all) allAggs.set(r.path, (all = newAgg()))
      merge(all, r)
    }
  }
  for (const r of allRows) {
    let all = allAggs.get(r.path)
    if (!all) { allAggs.set(r.path, (all = newAgg())); aggDepth.set(r.path, r.depth) }
    merge(all, r)
  }
  // Every region is a node — read ones from their rows, the rest valued
  // from the manifest — and so is each region's ancestor between P and it
  // (the kept set is ancestor-closed by construction); an ancestor whose U
  // slice never reached the threshold is unread and valued as its bands.
  for (const r of allRegions) {
    for (let q = r.path; q.length > path.length; q = parentOf(q)) {
      if (!aggDepth.has(q)) { aggDepth.set(q, q.split('/').length); mineAggs.set(q, null) }
    }
  }
  // Nothing is drawn inside an unread region (its total is a leaf until a
  // drill reads it), so U's attributed rows under one are not nodes.
  const unread = allRegions.filter(r => !regions.some(q => q.path === r.path)).map(r => r.path + '/')
  const insideUnread = (p: string) => unread.some(u => p.startsWith(u))
  for (const p of aggDepth.keys()) {
    if (unread.length && insideUnread(p)) continue
    const mine = mineAggs.get(p) ?? null
    const all = allAggs.get(p) ?? null
    // Reads return whole row groups, so U's attributed rows inside a read
    // region can sit below the threshold; the by-path read would have held
    // any path whose total clears it. One it didn't is under the fold.
    if (ol && !all && ol.needsTotal(p) && !ol.isAssigned(p)) continue
    aggs.set(p, ol ? scoped(p, ol.needsTotal(p) ? all : null, mine) : scoped(p, all, mine))
  }
  // An unread region and its unread ancestors already count its objects: `lensAgg` takes a node's
  // objects from the objects fold wherever it can (a region is assigned; an ancestor's bands hold the
  // regions under it). Only a node the fold leaves to the bytes' shape — inside a cover U holds every
  // byte but not every object of — adds the manifest's count of each unread region under it; adding it
  // anywhere else counts the region's objects twice.
  for (const r of allRegions) {
    if (regions.some(q => q.path === r.path)) continue
    const a0 = aggs.get(r.path)
    // An unread object region is a file: the assignment says so, and no row says otherwise.
    if (a0 && r.object && a0.kind == null) a0.kind = 'file'
    for (let q = r.path; q.length > path.length; q = parentOf(q)) {
      const a = aggs.get(q)
      if (a && mineAggs.get(q) == null && !allAggs.has(q) && ol!.o.needsTotal(q) && !ol!.o.isAssigned(q)) a.o += r.objects
    }
  }
  }
  // The interval store keeps read days in their own sort (specs/interval-store.md §2.2): the tiles' and
  // the root's, looked up as of the scan.
  if (plain && idx.asOf != null && !idx.schema.some(l => l.name === 'last_read')) {
    t0 = performance.now()
    const rootPaths = path === '' ? [...new Set(rootRows.map(r => r.path))] : [path]
    const lr = await ivLastRead(env, date, [...aggs.keys(), ...rootPaths])
    for (const [p, a] of aggs) { const d = lr.get(p); if (d != null) a.a = d }
    let ra: number | null = null
    for (const p of rootPaths) { const d = lr.get(p); if (d != null) ra = ra == null ? d : Math.max(ra, d) }
    rootAgg.a = ra
    tr?.('reads', performance.now() - t0, `${lr.size}`)
  }
  let matches: string[] | undefined
  if (query) {
    // Only a claims fold filters a partial read; a view root the query matches is matched whole.
    if (ol) noteCoverage(cov, 'approximate', APPROX_LENS)
    const f = nameFilter(
      { path, agg: rootAgg },
      new Map([...aggs].map(([p, agg]) => [p, { depth: aggDepth.get(p)!, agg }])),
      query,
      (into, from) => { for (const k of ['b', 'o', 'wts', 'wb'] as const) into[k] += from[k]; if (from.a != null) into.a = into.a == null ? from.a : Math.max(into.a, from.a); for (const key of ['cb', 'ub'] as const) for (const [k, v] of Object.entries(from[key])) into[key][k] = (into[key][k] ?? 0) + v },
      newAgg,
    )
    rootAgg = f.root
    aggs = new Map([...f.aggs].map(([p, e]) => [p, e.agg]))
    matches = [...f.matches].sort()
  }
  // Nodes with nothing in scope fold away entirely.
  for (const [p, a] of aggs) if (a.b <= 0) aggs.delete(p)
  const keptList = [...aggs.entries()].filter(([p, a]) => a.b >= thrAt(aggDepth.get(p)!))
  const truncated = keptList.length > HARD_CAP
  // Descendant-inclusive bytes are monotone (ancestor ≥ descendant), so the
  // kept set — and even its top-N-by-bytes truncation — is ancestor-closed:
  // every kept path's parent is kept (or is P itself). Linking is trivial.
  const kept = new Map(
    (truncated ? keptList.sort((a, b) => b[1].b - a[1].b).slice(0, HARD_CAP) : keptList),
  )
  // Sub-threshold child count per parent. A coarse tier only holds paths
  // above its floor, so this counts the folded children it can see; the
  // (other) bytes themselves are exact regardless (parent − Σ kept).
  for (const [p, a] of aggs) {
    if (kept.has(p)) continue
    if (a.b >= thrAt(aggDepth.get(p)!)) continue // truncation casualty, not a fold
    const par = parentOf(p)
    const key = kept.has(par) ? par : par === path || (path === '' && !p.includes('/')) ? path : null
    if (key !== null) foldedOf.set(key, (foldedOf.get(key) ?? 0) + 1)
  }
  return { rootAll, rootAgg, kept, aggDepth, foldedOf, threshold, thrAt, tier: tierName, idx, truncated, ...(matches ? { matches } : {}), ownerLens: ol, scoped }
}

/** A heavy literal's rollup at P nested on `date` (`rollupRead`, which draws it): every node known exactly, the
 *  paths answered, and what the budgets left unread. Null: nothing under P on `date`. */
export interface Nest {
  /** Every node known on `date`: its matched bytes and objects (exact), and its depth. */
  exact: Map<string, Agg>
  depthOf: Map<string, number>
  /** Per answered path, its children with matches on `date` (a rollup's by name; a forest's every one). */
  kidsOf: Map<string, string[]>
  /** A rollup's children holding roots on any scan (its fold count). */
  rollupKids: Map<string, number>
  /** The paths answered by their match roots (their forests are whole). */
  rootsDirs: Set<string>
  /** P and every path answered. */
  fetched: Set<string>
  skipped: { budget: number; late: number }
  total: Agg
  T: number
  thrAt: (d: number) => number
}

export interface NestOpts {
  date: string
  path: string
  dP: number
  rollup: Rollup
  /** A match root (its name holds the literal): drawn, never nested. */
  isRoot: (p: string) => boolean
  threshold?: number
  w: number
  h: number
  minArea: number
  atten: number
  maxDepth?: number
  /** The static source's answer at a path; absent: nothing nests. */
  fetch?: (p: string, opts: HitOpts) => Promise<Found | null>
  /** Paths to nest whatever their bytes and the budgets (`ViewOpts.nestForce`). */
  force?: Set<string>
  /** Answers (default `FILTER_NEST_READS`), decoded rows (default `FILTER_PHASE2_ROWS`), ms. */
  reads?: number
  rows?: number
  ms: number
  trace?: Trace
}

export async function nestRollup(o: NestOpts): Promise<Nest | null> {
  const { date, path, dP, w, h, minArea, atten, maxDepth, isRoot, rollup, trace: tr } = o
  const t0 = performance.now()
  /** Every node known on `date`, its matched bytes and objects (exact), and its depth. */
  const exact = new Map<string, Agg>()
  const depthOf = new Map<string, number>()
  /** Per answered path, its children with matches on `date` (a rollup's by name; a forest's every one); a
   *  rollup's children holding roots on any scan (its fold count); the paths answered by match roots. */
  const kidsOf = new Map<string, string[]>()
  const rollupKids = new Map<string, number>()
  const rootsDirs = new Set<string>()
  const addRollup = (dir: string, r: Rollup, d: number): Agg => {
    const { kids, rest } = rollupAt(r, date)
    const tot = newAgg()
    tot.b = Number(rest[0]); tot.o = Number(rest[1])
    const names: string[] = []
    for (const [c, b, ob] of kids) {
      const a = newAgg()
      a.b = Number(b); a.o = Number(ob)
      const cp = dir === '' ? c : `${dir}/${c}`
      if (!isRoot(cp)) a.kind = 'dir'
      exact.set(cp, a); depthOf.set(cp, d + 1); names.push(cp)
      tot.b += a.b; tot.o += a.o
    }
    kidsOf.set(dir, names)
    rollupKids.set(dir, r.children)
    return tot
  }
  /** `dir`'s match roots live on `date`, and every ancestor between `dir` and them (their sums). */
  const addRoots = (dir: string, hits: Hit[]) => {
    rootsDirs.add(dir)
    const D = scanAt(date)
    const fresh: string[] = []
    const node = (p: string, d: number, dir_: boolean): Agg => {
      let a = exact.get(p)
      if (!a) { exact.set(p, (a = newAgg())); depthOf.set(p, d); fresh.push(p); if (dir_) a.kind = 'dir' }
      return a
    }
    for (const hh of hits) {
      if (!(hh.vf <= D && D < hh.vt)) continue
      const size = Number(hh.size), n = Number(hh.n)
      const a = node(hh.path, hh.depth, false)
      a.b += size; a.o += n
      for (let q = parentOf(hh.path), d = hh.depth - 1; q !== dir && q !== ''; q = parentOf(q), d--) {
        const qa = node(q, d, true)
        qa.b += size; qa.o += n
      }
    }
    for (const p of fresh) {
      const par = parentOf(p)
      const ks = kidsOf.get(par)
      if (ks) ks.push(p); else kidsOf.set(par, [p])
    }
    if (!kidsOf.has(dir)) kidsOf.set(dir, [])
  }
  const total = addRollup(path, rollup, dP)
  if (total.b <= 0) return null
  const T = o.threshold ?? filterThreshold(total.b, w, h, minArea)
  const thrAt = (d: number) => T * atten ** Math.max(0, d - dP - 1)

  // --- the nest -------------------------------------------------------------------------------------
  const fetched = new Set<string>([path])
  const force = o.force
  const capD = dP + Math.min(FILTER_SUBDIV_LEVELS, maxDepth ?? Infinity)
  const subdivMin = T * (FILTER_SUBDIV_AREA / minArea)
  const wants = (p: string): boolean => {
    const a = exact.get(p)!, d = depthOf.get(p)!
    if (isRoot(p) || d >= capD) return false
    return !!force?.has(p) || (a.b >= thrAt(d) && a.b >= subdivMin && a.o > 1)
  }
  const skipped = { budget: 0, late: 0 }
  let reads = 0
  let rowsLeft = o.rows ?? FILTER_PHASE2_ROWS
  const fetch = o.fetch
  if (fetch && !rollup.bucketsOnly) {
    const ms = o.ms
    const late = Symbol('late')
    let timer: ReturnType<typeof setTimeout> | null = null
    let over = false
    const expired = new Promise<typeof late>(r => { timer = setTimeout(() => { over = true; r(late) }, ms) })
    let frontier = (kidsOf.get(path) ?? []).filter(wants)
    while (frontier.length) {
      // Heaviest first; a path the diff's other side read (`nestForce`) whatever the budget (it is held).
      frontier.sort((p, q) => exact.get(q)!.b - exact.get(p)!.b || (p < q ? -1 : p > q ? 1 : 0))
      const take: string[] = []
      for (const p of frontier) {
        if (force?.has(p)) take.push(p)
        else if (reads < (o.reads ?? FILTER_NEST_READS)) { take.push(p); reads++ }
        else skipped.budget++
      }
      const got = new Map<string, Found | null>()
      let i = 0
      const worker = async () => {
        while (i < take.length) {
          const p = take[i++]
          if (over) { skipped.late++; continue }
          const ho: HitOpts = force?.has(p) ? {} : { maxRows: Math.max(0, rowsLeft) }
          const f = fetch(p, ho)
          f.catch(() => {})
          const r = await Promise.race([f, expired])
          if (r === late) { skipped.late++; continue }
          if (ho.over) { skipped.budget++; continue }
          // A roots answer costs its roots (decoded once, its forest built per view); a rollup its few cells.
          if (r?.hits) rowsLeft -= r.hits.length
          got.set(p, r)
        }
      }
      await Promise.all(Array.from({ length: Math.min(NEST_PAR, take.length) }, worker))
      const next: string[] = []
      for (const p of take) {
        const f = got.get(p)
        // Declined (the path holds the literal under the hex-run rule) or not exact on this scan: one tile.
        if (!f || !covers(f, [date])) continue
        fetched.add(p)
        if (f.rollup) next.push(...(addRollup(p, f.rollup, depthOf.get(p)!), kidsOf.get(p)!.filter(wants)))
        else addRoots(p, f.hits)
      }
      frontier = next
    }
    if (timer != null) clearTimeout(timer)
    tr?.('nest', performance.now() - t0, `${fetched.size - 1} read, ${skipped.budget + skipped.late} left`)
  }
  return { exact, depthOf, kidsOf, rollupKids, rootsDirs, fetched, skipped, total, T, thrAt }
}

/** A heavy literal's view at P from P's rollup (`staticDrill.ts`) on `o.date`: each child of P holding match
 * roots is a cell of its matched bytes and objects — the kept children by name, the rest (and children under
 * the pixel budget) in `(other)`. Exact (the rollup's totals are), without owners. Children that are match
 * roots themselves (their name holds the literal: P's doesn't) are the view's `matched`, with their own rows'
 * kind, ages and classes looked up beside, as static roots' are; containers are directories.
 *
 * The drawn children nest (`fetch`, the static source's answer at a path): a child that is no match root, at
 * most `FILTER_SUBDIV_LEVELS` − 1 levels below P (and inside `maxDepth`), whose tile the plain filter view
 * would subdivide — over its level's threshold `T·atten^(d−dP−1)`, at least `FILTER_SUBDIV_AREA` px², more
 * than one object — is answered the same way: its rollup (heavy; its kept children by name, the rest its
 * `(other)`), whose drawn children nest in turn, or its match roots (light; their forest under it, every node
 * exact). Level by level, heaviest first, `NEST_PAR` at a time, at most `FILTER_NEST_READS` answers and phase 2's
 * rows (`FILTER_PHASE2_ROWS` match roots over the roots answers; one bounded past what is left is not read)
 * — past them a child stays one exact tile — within phase 2's time budget (`late`: kept briefly, upgraded); any
 * cut is `interiors`. Each answer spans every scan, so a diff's two sides read the same ones (`nestForce`). */
async function rollupRead(env: Env, o: ViewOpts, rollup: Rollup, x: { dP: number; rootAll: Agg; idx: IndexHandle; details: IndexHandle; trace?: Trace; fetch?: (p: string, opts: HitOpts) => Promise<Found | null> }): Promise<Read | null> {
  const { date, path, w, h, minArea, atten, query, maxDepth } = o
  const { dP, trace: tr } = x
  const t0 = performance.now()
  const isRoot = (p: string) => !!query?.(p)
  const nest = await nestRollup({
    date, path, dP, rollup, isRoot, threshold: o.threshold, w, h, minArea, atten, maxDepth,
    fetch: o.firstPaint || o.floorOnly ? undefined : x.fetch, force: o.nestForce, reads: o.nestReads, rows: o.phase2Rows,
    ms: o.phase2Ms ?? (Number(env.FILTER_PHASE2_MS) || FILTER_PHASE2_MS), trace: tr,
  })
  if (!nest) return null
  const { exact, depthOf, kidsOf, rollupKids, rootsDirs, fetched, skipped, total, T, thrAt } = nest

  // The drawn tree: every known node over its level's threshold whose parent is drawn (or is P), within `maxDepth`.
  const kept = new Map<string, Agg>()
  const aggDepth = new Map<string, number>()
  const draw = (dir: string) => {
    for (const cp of kidsOf.get(dir) ?? []) {
      const a = exact.get(cp)!, d = depthOf.get(cp)!
      if (maxDepth != null && d > dP + maxDepth) continue
      if (a.b >= thrAt(d)) { kept.set(cp, a); aggDepth.set(cp, d); draw(cp) }
    }
  }
  draw(path)
  const foldedOf = new Map<string, number>()
  const capped = capTiles(kept, aggDepth, path, o.maxTiles ?? tileBudget(w, h))
  for (const dir of [path, ...kept.keys()]) {
    const ks = kidsOf.get(dir)
    if (!ks) continue
    const n = ks.filter(k => kept.has(k)).length
    const f = rollupKids.has(dir) ? rollupKids.get(dir)! - n : ks.length - n
    if (f > 0) foldedOf.set(dir, f)
  }
  if (!foldedOf.has(path)) foldedOf.set(path, 0)
  const matched = [...exact].filter(([p]) => isRoot(p)).map(([p, a]) => ({ path: p, b: a.b, o: a.o })).sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : x.path > y.path ? 1 : 0))
  // The drawn match roots: their own rows (kind, ages, classes, owners — the whole root is matched) by point
  // lookups, heaviest first, within `FILTER_DETAILS_MS`; never on the first paint.
  const want = matched.filter(m => kept.has(m.path)).slice(0, ROOT_DETAILS)
  if (want.length && !o.firstPaint && !(maxDepth != null && maxDepth <= 0)) {
    const asks = new Set(want.map(m => `${depthOf.get(m.path)}\0${m.path}`))
    const look = readAsks(x.details, want.map(m => ({ depth: depthOf.get(m.path)!, path: m.path })), r => asks.has(`${r.depth}\0${r.path}`), { maxGroups: 60 }).then(r => r.rows).catch(e => {
      if (!/too wide/.test(String((e as Error).message ?? e))) throw e
      tr?.('details', 0, 'too wide')
      return [] as Row[]
    })
    const budget = Number(env.FILTER_DETAILS_MS) || Infinity
    const late = Symbol('late')
    const rows = budget === Infinity ? await look : await Promise.race([look, new Promise<typeof late>(r => setTimeout(() => r(late), budget))]).then(v => {
      if (v !== late) return v
      tr?.('details', performance.now() - t0, 'late')
      look.catch(() => {})
      return [] as Row[]
    })
    const own = new Map<string, Agg>()
    for (const r of rows) {
      let a = own.get(r.path)
      if (!a) own.set(r.path, (a = newAgg()))
      merge(a, r)
    }
    for (const [p, d] of own) {
      const a = exact.get(p)!
      a.kind = d.kind; a.wts = d.wts; a.wb = d.wb; a.a = d.a; a.cb = d.cb; a.ub = d.ub; a.nc = d.nc; a.nd = d.nd
    }
    tr?.('details', performance.now() - t0, String(own.size))
  }
  tr?.('rollup', performance.now() - t0, `${kidsOf.get(path)!.length} kids, ${kept.size} kept${capped.length ? `, ${capped.length} over the tile budget` : ''}`)
  const left = skipped.budget + skipped.late
  // A path is known whole on this side (a diff takes its children from `exact`, absent = zero): it was answered,
  // or it sits inside a roots answer above every match root, or it has no matches here under a known parent.
  const complete = (p: string): boolean => {
    if (fetched.has(p)) return true
    for (let q = p; q.length > path.length; q = parentOf(q)) {
      if (isRoot(q)) return false
      const par = parentOf(q)
      if (rootsDirs.has(par)) return true
      if (fetched.has(par)) break
    }
    return p !== path && !exact.has(p) && complete(parentOf(p))
  }
  return {
    rootAll: x.rootAll, rootAgg: total, kept, aggDepth, foldedOf,
    threshold: T, thrAt, tier: 'rollup', idx: x.idx, truncated: false,
    matches: matched.map(m => m.path).sort(), matched: matched.map(m => ({ path: m.path, b: Math.round(m.b), o: Math.round(m.o) })),
    matchCount: { n: rollup.rows ?? matched.length, b: Math.round(total.b), o: Math.round(total.o) },
    rollup: { children: rollup.children, kept: rollup.kept, rows: rollup.rows, ...(rollup.bucketsOnly ? { bucketsOnly: true as const } : {}), ...(rollup.scopedBelow ? { scopedBelow: true as const } : {}) },
    exact, complete, nested: fetched,
    ...(left ? { interiors: { read: fetched.size - 1, skipped: left, reason: [skipped.budget && `${skipped.budget} over the read budget`, skipped.late && `${skipped.late} past the time budget`].filter(Boolean).join(', '), ...(skipped.late ? { late: skipped.late } : {}) } } : {}),
    ...(o.firstPaint ? { firstPaint: true } : {}), ownerLens: null, scoped: (_p, _all, mine) => mine!,
  }
}

/** P's own row(s) from the by-path tiers — coarsest that has it, else fine. */
async function readRootAllRows(env: Env, date: string, path: string, dP: number): Promise<Row[]> {
  const read = (idx: IndexHandle) => (path === '' ? readRows(idx, 1, 1, '', '￿') : readRows(idx, dP, dP, path, path))
  for (const e of COARSE_EXPS) {
    const idx = await tryOpen(env, date, `coarse${e}`)
    if (!idx) continue
    const rows = await read(idx)
    if (rows.length) return rows
  }
  return read(await openFine(env, date, 'path'))
}

/** Bumped when a filtered view's answer changes for the same inputs (the subtree and diff cache keys carry
 *  it with `q=`): 2 — phase 2 bounded (subdivision area, levels, read and time budgets), the tile budget;
 *  3 — a view the query matches draws its subtree again; 4 — `depth=N` caps phase 2 at dP + N;
 *  5 — level-capped interiors from the `path` sort; 6 — `\^`, `\$`, `\\` escapes in a `simple` term (`q=` is keyed raw). */
export const FILTER_VIEW_V = 6

/** A filter view's phase 2 (the insides of its match roots) subdivides a root only when its tile is at
 *  least this many px² — room for a title and a few legible cells; a smaller root is one exact tile. */
export const FILTER_SUBDIV_AREA = 64 * 64
/** …to at most this many levels below the root (rows at the last level come back childless: drillable). */
export const FILTER_SUBDIV_LEVELS = 3
/** …reading at most this many row groups and rows (admitted heaviest root depth first; a read past either
 *  is skipped): each group is a ranged GET (six in flight per request) and its decode is the isolate's CPU —
 *  ~60 ms per 32K-row group on the edge, all of it after the reads' clocks stop (`tmp` under a bucket's
 *  `checkpoints`: 24 groups, 0.6 s of Server-Timing, 2 s to the client). */
export const FILTER_PHASE2_GROUPS = 16
export const FILTER_PHASE2_ROWS = 160_000
/** …a depth's roots over that budget together are planned one by one, its heaviest this many. */
export const FILTER_SPLIT_ROOTS = 4
/** …within this many ms (`FILTER_PHASE2_MS` overrides): a read still running then is dropped. With the
 *  root details' wait past it (`FILTER_DETAILS_MS`, dev 300) and the build, a full view lands in ~2 s. */
export const FILTER_PHASE2_MS = 1500
/** A heavy literal's rollup view nests at most this many static answers (`rollupRead`), `NEST_PAR` at a
 *  time: each is a few ranged GETs per drill tier (an index span, a data span), held per isolate after. */
export const FILTER_NEST_READS = 48
const NEST_PAR = 6
/** px² per drawn tile: a view draws at most w·h / this many nodes (1280×768: 10,240 — what the plain
 *  view's busiest paths draw). */
export const TILE_AREA = 96

/** The most nodes a w×h canvas draws (`TILE_AREA` px² each). */
export const tileBudget = (w: number, h: number): number => Math.max(1, Math.floor((w * h) / TILE_AREA))

/** Cut `kept` (ancestor-closed under `path`) to its `budget` heaviest nodes, ancestor-closed still: by
 *  bytes, ties shallowest first, then by path — an ancestor holds at least its descendants' bytes, so it
 *  ranks ahead of them — and a node whose parent fell out goes with it. Mutates `kept`; returns the
 *  paths it dropped. Exact either way: a dropped node's bytes stay in its parent's `(other)`. */
export function capTiles(kept: Map<string, { b: number }>, depth: Map<string, number>, path: string, budget: number): string[] {
  if (kept.size <= budget) return []
  const order = [...kept.keys()].sort((x, y) => kept.get(y)!.b - kept.get(x)!.b || depth.get(x)! - depth.get(y)! || (x < y ? -1 : x > y ? 1 : 0))
  const keep = new Set(order.slice(0, budget))
  for (const p of [...keep].sort((x, y) => depth.get(x)! - depth.get(y)!)) {
    const par = parentOf(p)
    if (par !== path && !keep.has(par)) keep.delete(p)
  }
  const dropped = order.filter(p => !keep.has(p))
  for (const p of dropped) kept.delete(p)
  return dropped
}

/** Assigned regions read (largest first) per lens view; the rest are
 * manifest-valued leaves. Two span queries' worth of rects. */
const REGION_READS = 24
/** Phase 1 of a static literal's view per isolate: the held hits array (`staticFilter.ts` keeps it while the
 *  literal is held) → per scan and owner scope, its aggregates and sorted roots. Read-only once built. */
const staticPhase1 = new WeakMap<Hit[], Map<string, Phase1Memo>>()

/** A static literal's phase 1, held per isolate, and (unscoped, nothing excluded) everything about its
 *  forest that the pixel budget doesn't decide, built on first use: its roots as a set, every forest node's
 *  depth and bytes (roots and their ancestors under P), each ancestor's aggregate and the roots' total, and
 *  the match list heaviest first. A view then touches only the nodes it draws (`par` under a bucket's
 *  `checkpoints`: ~80K roots, ~1 s of edge CPU per view without it). Read-only once built. */
interface Phase1Memo {
  p1: { all: Map<string, Agg>; mine: Map<string, Agg>; depth: Map<string, number> }
  roots: string[]
  rootSet?: Set<string>
  forest?: { depth: Map<string, number>; nb: Map<string, number>; sums: Map<string, Agg>; total: Agg; matched: { path: string; b: number; o: number }[] }
}

/** A map read through to `base` where it holds no entry of its own (writes stay here). */
class Overlay<K, V> extends Map<K, V> {
  constructor(readonly base: Map<K, V>) { super() }
  override get(k: K): V | undefined { return super.has(k) ? super.get(k) : this.base.get(k) }
  override has(k: K): boolean { return super.has(k) || this.base.has(k) }
}

/** `from` summed into `into` (`readView`'s `sumAgg`). */
function addAgg(into: Agg, from: Agg): void {
  into.b += from.b; into.o += from.o; into.wts += from.wts; into.wb += from.wb
  if (from.a != null) into.a = into.a == null ? from.a : Math.max(into.a, from.a)
  for (const k in from.cb) into.cb[k] = (into.cb[k] ?? 0) + from.cb[k]
  for (const k in from.ub) into.ub[k] = (into.ub[k] ?? 0) + from.ub[k]
}

/** The memo's forest (`Phase1Memo.forest`) under `path` (depth `dP`). */
function buildForest(m: Phase1Memo, path: string, dP: number): NonNullable<Phase1Memo['forest']> {
  const { mine, depth: rd } = m.p1
  const depth = new Map<string, number>()
  const nb = new Map<string, number>()
  const levels: string[][] = []
  const total = newAgg()
  for (const r of m.roots) {
    const a = mine.get(r)!, d = rd.get(r)!
    depth.set(r, d); nb.set(r, a.b); (levels[d] ??= []).push(r)
    addAgg(total, a)
  }
  const sums = new Map<string, Agg>()
  for (const r of m.roots) {
    for (let q = parentOf(r), d = rd.get(r)! - 1; q.length > path.length; q = parentOf(q), d--) {
      if (nb.has(q)) break
      nb.set(q, 0); depth.set(q, d); sums.set(q, newAgg()); (levels[d] ??= []).push(q)
      if (q === '') break
    }
  }
  for (let d = levels.length - 1; d > dP + 1; d--) {
    for (const p of levels[d] ?? []) {
      const par = parentOf(p)
      nb.set(par, nb.get(par)! + nb.get(p)!)
      addAgg(sums.get(par)!, sums.get(p) ?? mine.get(p)!)
    }
  }
  const matched = m.roots.map(r => ({ path: r, b: Math.round(mine.get(r)!.b), o: Math.round(mine.get(r)!.o) }))
    .sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : x.path > y.path ? 1 : 0))
  return { depth, nb, sums, total, matched }
}

/** Static match roots whose own rows (kind, ages, classes) are looked up per view, heaviest first. */
const ROOT_DETAILS = 48
/** …from at most this many row groups (wider declines after the span plan, fetching nothing): the lookups
 *  share the request's six connections with phase 2, and roots spread over many depths (`00241`) took
 *  ~20 groups to answer after phase 2 had already given up on them. */
const DETAIL_GROUPS = 16

/** `xs` in code-unit order: itself when it already is (a static literal's ~80K roots are), else a sorted copy. */
function sortedOnce(xs: string[]): string[] {
  for (let i = 1; i < xs.length; i++) if (xs[i - 1] > xs[i]) return [...xs].sort()
  return xs
}

const parentOf = (p: string): string => {
  const cut = p.lastIndexOf('/')
  return cut === -1 ? '' : p.slice(0, cut)
}

/** Kept paths grouped under their parent (or P for the top level). */
function kidsIndex(kept: Map<string, Agg>, path: string): Map<string, string[]> {
  const kidsOf = new Map<string, string[]>()
  for (const [p] of kept) {
    const key = kept.has(parentOf(p)) ? parentOf(p) : path
    const arr = kidsOf.get(key)
    if (arr) arr.push(p)
    else kidsOf.set(key, [p])
  }
  return kidsOf
}

/** The store root's crumb label: `ROOT_LABEL` (wrangler var) per deployment. */
const rootName = (path: string, env?: Env) => (path === '' ? env?.ROOT_LABEL ?? 'all buckets' : path.split('/').pop()!)

/** A read scoped by owner or class needs owner-slice rows: the interval store's slice sorts (`slices`). */
const sliced = (env: Env, o: { lens?: Lens; owner?: OwnerScope; classes?: ClassScope; by?: string }): Env => (o.lens || o.owner || o.classes || o.by ? slices(env) : env)

export async function buildView(env0: Env, o: ViewOpts): Promise<View> {
  const env = sliced(asOfScans(env0, [o.date]), o)
  const { path, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const cov: Coverage = {}
  const t0 = performance.now()
  // The provenance sidecar, beside the read (its own `extras` term in the trace: the served seam's).
  const [v, ex] = await Promise.all([readView(env, o, cov), extrasFor(env, o.date, path).then(x => { o.trace?.('extras', performance.now() - t0); return x })])
  if (!v) {
    return { tree: { n: rootName(path, env), k: 'dir', b: 0, o: 0 }, tier: 'none', index: 'none', threshold: 0, nodes: 0, truncated: false, ...(query ? { matches: [], matched: [], ...coverageFields(cov) } : {}) }
  }
  const { kept, aggDepth, foldedOf, thrAt } = v
  const nodeOf = (name: string, a: Agg): ViewNode => ({
    n: name,
    k: a.kind ?? 'dir',
    b: Math.round(a.b),
    o: Math.round(a.o),
    ...display(a),
  })
  const kidsOf = kidsIndex(kept, path)
  const matched = v.matchSet ?? new Set(v.matches ?? [])
  const build = (p: string, a: Agg): ViewNode => {
    const node = nodeOf(p === path ? rootName(path, env) : p.split('/').pop()!, a)
    if (matched.has(p)) node.m = 1
    // Index extras: the top owner's provenance, when the scan's generation
    // carries the sidecar.
    if (ex) {
      let top: string | null = null
      let topB = 0
      for (const [u, b] of Object.entries(a.ub)) if (b > topB) { top = u; topB = b }
      const pv = top ? ex.provenance(p, top) : null
      if (pv) node.pv = pv
    }
    const childPaths = kidsOf.get(p) ?? []
    if (!childPaths.length) return node
    const kids = childPaths
      .map(cp => build(cp, kept.get(cp)!))
      .sort((x, y) => y.b - x.b)
    const kidAggs = childPaths.map(cp => kept.get(cp)!)
    const rest = subtract(a, kidAggs)
    node.c = kids
    if (rest.b > thrAt((aggDepth.get(childPaths[0]!) ?? dP + 1))) {
      // What the fold stands in for: every child not named — objects and
      // dirs — where the row says how many children there are; else the
      // sub-threshold children the read saw (a v1 tier holds dirs only).
      const f = a.nc != null ? Math.max(0, a.nc - childPaths.length) : foldedOf.get(p) ?? 0
      node.c.push({ ...nodeOf('(other)', rest), f } as ViewNode)
    }
    return node
  }
  const tree = build(path, v.rootAgg)
  return { tree, tier: v.tier, index: v.idx.asOf != null ? `iv:${env.INTERVAL_STORE_GEN}` : v.idx.mode, threshold: v.threshold, nodes: kept.size, truncated: v.truncated, ...(v.matched ? { ...matchLists(v.matched, kept), ...(v.matchCount ? { matchCount: v.matchCount, matchesCapped: true as const } : {}) } : v.matches ? { matches: v.matches } : {}), ...(v.rollup ? { rollup: v.rollup } : {}), ...(v.excluded ? { excluded: v.excluded } : {}), ...(v.firstPaint ? { firstPaint: true } : {}), ...(v.interiors ? { interiors: v.interiors } : {}), ...(query ? coverageFields(cov) : {}) }
}

/** The most match roots a response lists (`matches` / `matched`) beyond those the tree draws. */
export const MATCH_LIST_CAP = 200

/** A response's match lists: every root the tree keeps, then the heaviest others up to
 * `MATCH_LIST_CAP`, and the whole set's count and totals. */
export function matchLists(matched: { path: string; b: number; o: number }[], kept: Map<string, unknown>): Pick<View, 'matches' | 'matched' | 'matchCount' | 'matchesCapped'> {
  const count = { n: matched.length, b: matched.reduce((n, m) => n + m.b, 0), o: matched.reduce((n, m) => n + m.o, 0) }
  if (matched.length <= MATCH_LIST_CAP) return { matches: matched.map(m => m.path).sort(), matched, matchCount: count }
  let room = MATCH_LIST_CAP
  const list = matched.filter(m => kept.has(m.path) || room-- > 0)
  return { matches: list.map(m => m.path).sort(), matched: list, matchCount: count, matchesCapped: true }
}

// --- the diff: two scans, one byte floor -----------------------------------

export interface DiffOpts extends Omit<ViewOpts, 'date'> {
  from: string
  to: string
  /** Frontier rows kept (largest |Δ| first); the expanded skeleton always ships. */
  top: number
  /** Totals only: both sides' scoped root reads, no walk (`rows` empty) —
   * what the diff section's headline shows while the full diff aligns. */
  summary?: boolean
  /** Expand at most this many levels below P (rows at the cap come back
   * unexpanded, as leaves) — `depth=1` is the bucket-level diff the page
   * draws first, before the full walk lands. */
  depth?: number
}

export interface DiffRow {
  /** Path relative to P (`(other)` = the fold under its parent). */
  p: string
  d: number
  /** What the path is on whichever side has it; a fold is `dir`. */
  k: 'file' | 'dir'
  s: 'added' | 'removed' | 'changed' | 'unchanged'
  a: number
  b: number
  oa: number
  ob: number
  /** Expanded: its own Δ is carried by its children. */
  x?: true
  /** Read by a point lookup on the side where it sat under the floor. */
  l?: 1 | 2
}

export interface Diff {
  rows: DiffRow[]
  total_a: number
  total_b: number
  objects_a: number
  objects_b: number
  expansions: number
  truncated: boolean
  threshold: number
  tier: string
  lookups: number
  /** The lookup budget ran out: some one-sided names may really be folded. */
  lookups_capped: boolean
  /** …by time (`FILTER_WALK_MS`). */
  lookups_late?: true
  /** With `q=`: the union of both scans' match roots (capped like a view's, `matchLists`). */
  matched?: { path: string; b: number; o: number }[]
  matchCount?: { n: number; b: number; o: number }
  matchesCapped?: true
  /** With `q=`: either side's phase 2 left roots undivided (`View.interiors`), summed over the sides that did. */
  interiors?: { read: number; skipped: number; reason: string; late?: number }
  /** With `q=`: either side's `partial` / `approximate` (`View`), reasons
   * merged. */
  partial?: true
  partialReason?: string
  approximate?: true
  approximateReason?: string
  /** With `q=`: either side is a dir-only scan (`View.dirsOnly`; both are, or the diff is refused). */
  dirsOnly?: true
}

const LOOKUP_CAP = 240
/** A filtered diff's one-sided lookups run this long, at most (each level's are one batched read a side). */
export const FILTER_WALK_MS = 800
/** …and its two sides' phase 2 this long (each; they run at once). */
export const FILTER_DIFF_PHASE2_MS = 900

/** Two sides' `interiors`, summed (reasons joined when they differ). */
function sumInteriors(a?: View['interiors'], b?: View['interiors']): NonNullable<View['interiors']> {
  const xs = [a, b].filter((x): x is NonNullable<View['interiors']> => !!x)
  const late = xs.reduce((n, x) => n + (x.late ?? 0), 0)
  return { read: xs.reduce((n, x) => n + x.read, 0), skipped: xs.reduce((n, x) => n + x.skipped, 0), reason: [...new Set(xs.map(x => x.reason))].join(' · '), ...(late ? { late } : {}) }
}

/** What changed under P between two scans (specs/view-serving.md §2):
 * both sides are read from their index tiers at ONE absolute byte floor —
 * the larger side's pixel-budget threshold — so a directory is named on both
 * sides or folded on both, never named here and hidden in `(other)` there
 * (the artifact a per-side budget fold produces: a static 337 Ti dir
 * reading as −81 / +141 Ti of churn). A name that clears the floor on one
 * side only is read point-blank from the other side's floor-free tier: if
 * it exists there it shows with its real size (it crossed the floor), else
 * it was added / removed. `(other)` is parent − Σ named on each side, so its
 * Δ is the sub-floor churn, truthfully. */
export async function buildDiff(env0: Env, o: DiffOpts): Promise<Diff> {
  const env = sliced(asOfScans(env0, [o.from, o.to]), o)
  const { from, to, path, lens, owner, query } = o
  const dP = path === '' ? 0 : path.split('/').length
  const tr = o.trace
  // A literal's matches on a dir-only scan are its folders', on a full one its files' too: not comparable.
  const split = query && !lens && !o.classes ? await dirsOnlySplit(staticFilterStore(env), query.ast, path, [from, to]) : null
  if (split) throw new FilterRejected(split)
  let t0 = performance.now()
  const [ra, rb] = await Promise.all([
    readRootAgg(env, { date: from, path, lens }),
    readRootAgg(env, { date: to, path, lens }),
  ])
  tr?.('rootagg', performance.now() - t0)
  if (!ra && !rb) throw new NotFound(path)
  const threshold = o.threshold ?? (Math.max(ra?.b ?? 0, rb?.b ?? 0) * o.minArea) / (o.w * o.h)
  t0 = performance.now()
  // A depth-capped walk only ever consults rows down to that depth, so the
  // views read just those bands (`depth=1`: two groups instead of ~25 a side).
  // A filtered summary needs only the match roots' totals (phase 1), not the forest under them — unless
  // exclusions, which phase 2 also finds.
  const cap = o.summary && query && !query.neg ? { maxDepth: 0 } : o.depth != null ? { maxDepth: o.depth } : {}
  // Either side's coverage notes, merged (a re-read below adds the same ones).
  const cov: Coverage = {}
  // A filtered diff plans each side's forest by its own matched bytes and draws both at the larger side's
  // floor: the match roots alone (phase 1, `maxDepth: 0` — the static index's are held per isolate) say
  // each side's floor, so both forests are read once, at the shared one. (Reading both first and then
  // re-reading the lighter side at the shared floor paid that side's phase 2 twice.)
  let floor: number | undefined
  // A diff draws its roots' kinds only (no ages, no classes): the lookups that land by phase 2's end are
  // taken, none waited for (with a deployment's finite `FILTER_DETAILS_MS`; unset waits, as views do).
  // Its phase 2 runs both sides at once, then the walk's lookups (`FILTER_WALK_MS`): a smaller share each
  // (`o.phase2Ms`, a background full run's, overrides).
  const wait = { ...(Number(env.FILTER_DETAILS_MS) ? { detailsWait: 0 } : {}), phase2Ms: o.phase2Ms ?? Math.min(Number(env.FILTER_PHASE2_MS) || FILTER_PHASE2_MS, FILTER_DIFF_PHASE2_MS) }
  // Every read of this diff shares one phase-2 gate: a side whose round read nothing in time stops the
  // re-read at the shared floor from paying its budget again.
  const gate: Phase2Gate = o.phase2Gate ?? { dead: false }
  if (query && ra && rb && !o.summary && o.threshold == null) {
    const [ta, tb] = await Promise.all([
      readView(env, { ...o, date: from, maxDepth: 0, floorOnly: true }, {}),
      readView(env, { ...o, date: to, maxDepth: 0, floorOnly: true }, {}),
    ])
    if (ta && tb) floor = Math.max(ta.threshold, tb.threshold)
    tr?.('floors', performance.now() - t0)
  }
  const sideOpts = (date: string): ViewOpts => ({ ...o, date, ...wait, phase2Gate: gate, ...(query ? floor != null ? { threshold: floor } : {} : { threshold }), ...cap })
  let [va, vb] = await Promise.all([
    ra ? readView(env, sideOpts(from), cov) : null,
    rb ? readView(env, sideOpts(to), cov) : null,
  ])
  // Without the pre-pass (a summary, or one side without matches then), a side whose floor differs is
  // re-read at the larger.
  if (query && va && vb && va.threshold !== vb.threshold) {
    const shared = Math.max(va.threshold, vb.threshold)
    if (va.threshold < shared) va = await readView(env, { ...o, date: from, threshold: shared, phase2Gate: gate, ...cap }, cov)
    else vb = await readView(env, { ...o, date: to, threshold: shared, phase2Gate: gate, ...cap }, cov)
  }
  // A heavy literal's rollup sides nest by their own bytes: each side is read again nesting every path either
  // answered (`nestForce`; the answers span both scans and are held, so the second read is the first's), and a
  // path still answered on one side only (the time budget) is drawn as one tile on both.
  if (va?.nested && vb?.nested) {
    const union = new Set([...va.nested, ...vb.nested])
    const again = (v: Read, date: string) => v.nested!.size < union.size ? readView(env, { ...sideOpts(date), threshold: v.threshold, nestForce: union }, cov) : v
    ;[va, vb] = await Promise.all([again(va, from), again(vb, to)])
    if (va?.nested && vb?.nested) for (const [v, w] of [[va, vb], [vb, va]] as const) {
      const one = [...v.nested!].filter(p => !w.nested!.has(p))
      if (one.length) for (const p of [...v.kept.keys()]) if (one.some(q => p.startsWith(q + '/'))) v.kept.delete(p)
    }
  }
  tr?.('views', performance.now() - t0)
  // Nothing in scope on either side (a filter with no matches, an empty owner
  // pool): an empty diff, not a crash on the missing views.
  if (!va && !vb) {
    return { rows: [], total_a: 0, total_b: 0, objects_a: 0, objects_b: 0, threshold: 0, tier: 'none', ...(query ? { matched: [], ...coverageFields(cov) } : {}), expansions: 0, truncated: false, lookups: 0, lookups_capped: false }
  }
  // One side a v1 generation (dirs only), the other a store (objects too):
  // the objects have no counterpart to be compared with, so they fold into
  // `(other)` on the store side rather than reading as added / removed.
  if (va && vb && isStore(va.idx) !== isStore(vb.idx)) {
    for (const v of [va, vb]) for (const [p, a] of v.kept) if (a.kind === 'file') v.kept.delete(p)
  }
  const walkStart = performance.now()
  // A filtered diff's lookups stop after `FILTER_WALK_MS` as they do past `LOOKUP_CAP` (`lookups_capped`).
  let late = false
  let lookupsOff = false
  const walkOver = () => (late ||= !!query && performance.now() - walkStart > FILTER_WALK_MS)
  const matchedUnion = query ? [...new Map([...(va?.matched ?? []), ...(vb?.matched ?? [])].map(m => [m.path, m])).values()].sort((x, y) => x.path < y.path ? -1 : 1) : undefined
  const totals = {
    total_a: Math.round(va?.rootAgg.b ?? 0),
    total_b: Math.round(vb?.rootAgg.b ?? 0),
    objects_a: Math.round(va?.rootAgg.o ?? 0),
    objects_b: Math.round(vb?.rootAgg.o ?? 0),
    threshold: query ? ((vb ?? va)!.threshold) : threshold,
    tier: (vb ?? va)!.tier,
    ...(matchedUnion ? (() => {
      const keptBoth = new Map([...(va?.kept ?? []), ...(vb?.kept ?? [])])
      const { matched, matchCount, matchesCapped } = matchLists([...matchedUnion].sort((x, y) => y.b - x.b || (x.path < y.path ? -1 : 1)), keptBoth)
      // A rollup side lists only its kept children that are roots: its count is the rollup's bound.
      const roll = vb?.matchCount ?? va?.matchCount
      return { matched: matched!.sort((x, y) => x.path < y.path ? -1 : 1), matchCount: roll ?? matchCount, ...(matchesCapped || roll ? { matchesCapped: true as const } : {}) }
    })() : {}),
    ...(va?.interiors || vb?.interiors ? { interiors: sumInteriors(va?.interiors, vb?.interiors) } : {}),
    ...(query ? coverageFields(cov) : {}),
  }
  if (o.summary) return { rows: [], ...totals, expansions: 0, truncated: false, lookups: 0, lookups_capped: false }
  const kidsA = va ? kidsIndex(va.kept, path) : new Map<string, string[]>()
  const kidsB = vb ? kidsIndex(vb.kept, path) : new Map<string, string[]>()

  // Point lookups on the floor-free tier, memoized per side.
  const fineOf = new Map<string, Promise<IndexHandle>>()
  const fine = (date: string) => {
    let h = fineOf.get(date)
    if (!h) fineOf.set(date, (h = (lens ? lensSort(env, date) : Promise.resolve('path')).then(s => openFine(env, date, s, lens)).then(x => withTrace(x, tr))))
    return h
  }
  const fineAllOf = new Map<string, Promise<IndexHandle>>()
  const fineAll = (date: string) => {
    let h = fineAllOf.get(date)
    if (!h) fineAllOf.set(date, (h = openFine(env, date, 'path')))
    return h
  }
  let lookups = 0
  let capped = false
  // Each side's match roots and excluded paths as sets (a static literal's ~80K roots: an ancestor walk per
  // lookup, not a scan of every root).
  const sets = new Map<Read, { m: Set<string>; e: Set<string> }>()
  const setsOf = (v: Read) => { let x = sets.get(v); if (!x) sets.set(v, (x = { m: new Set(v.matches ?? []), e: new Set(v.excluded ?? []) })); return x }
  const atOrUnder = (p: string, set: Set<string>): boolean => {
    if (!set.size) return false
    for (let q = p; ; q = parentOf(q)) { if (set.has(q)) return true; if (q === '' || q.length <= path.length) return false }
  }
  const inQuery = (p: string, v: Read): boolean =>
    !query || (atOrUnder(p, setsOf(v).m) && !atOrUnder(p, setsOf(v).e))
  // A path's lookup total less the side's excluded paths under it (NOT).
  const net = (v: Read, p: string, a: Agg): Agg => {
    if (!v.excl) return a
    const cut = newAgg()
    for (const [e, ea] of v.excl) if (p === '' || e.startsWith(p + '/')) { cut.b += ea.b; cut.o += ea.o; cut.wts += ea.wts; cut.wb += ea.wb; for (const key of ['cb', 'ub'] as const) for (const [k, x] of Object.entries(ea[key])) cut[key][k] = (cut[key][k] ?? 0) + x }
    if (!cut.b && !cut.o) return a
    const out = subtract(a, [cut])
    out.kind = a.kind
    return out
  }
  const lookup = async (date: string, v: Read, p: string, depth: number): Promise<Agg | null> => {
    if (!inQuery(p, v)) return null
    if (lookups >= LOOKUP_CAP || walkOver()) { capped = true; return null }
    lookups++
    const rows = await readRows(await fine(date), depth, depth, p, p, undefined, lens)
    const needTot = !!v.ownerLens?.needsTotal(p)
    const allRows = needTot ? await readRows(await fineAll(date), depth, depth, p, p) : rows
    if (!rows.length && !allRows.length) return null
    const all = newAgg()
    const mine = newAgg()
    for (const r of allRows) merge(all, classRow(r, o.classes))
    for (const r of rows) if (ownerOk(r.usr, owner)) merge(mine, classRow(r, o.classes))
    const a = net(v, p, v.scoped(p, v.ownerLens && !needTot ? null : all, mine))
    return a.b > 0 ? a : null
  }

  // One side's lookups for a whole level in one read: `readAsks` turns the
  // level's (depth, path) asks into a few span queries + one round of group
  // reads, where the per-ask `lookup` cost two D1 queries and a range read
  // each, eight at a time — 150 lookups was ~20 s of a 7-day root diff
  // (2026-09-15). Lens views keep the per-ask path: the user-keyed index's
  // group stats only hold inside a single-user group, which `readAsks`'s
  // per-depth rectangles don't model. A batch too wide for the reader's group
  // cap falls back to per-ask reads rather than failing the diff.
  // Both sides read as of their scans from one interval store, unscoped and unfiltered: a path's row is
  // one version, and its versions never overlap. So a name one side lacks is the other side's very row
  // when that version is live at this side's scan (no read), and otherwise a version that closed by the
  // other's opened (`vtLe`) or opened once it closed (`vfGe`): the open segment and the dyadic blocks
  // outside that window are never read. (Unbounded, 10-08 → 10-09 at the root reads 12 groups to find
  // 17 added names absent, none of them holding an answer.)
  const sameStore = !!(va && vb && va.idx.asOf != null && vb.idx.asOf != null && va.idx.gen === vb.idx.gen && !query && !lens && !owner && !o.classes && !o.by)
  const lookupMany = async (date: string, v: Read, items: { cp: string; d: number; other?: Agg }[]): Promise<Map<string, Agg | null>> => {
    const out = new Map<string, Agg | null>()
    const todo: { cp: string; d: number; vtLe?: number; vfGe?: number }[] = []
    const D = sameStore ? v.idx.asOf! : null
    for (const q of items) {
      if (!inQuery(q.cp, v)) { out.set(q.cp, null); continue }
      const w = D != null ? q.other?.ver : null
      if (!w) { todo.push(q); continue }
      if (w.vf <= D! && D! < w.vt) { out.set(q.cp, q.other!); continue }
      todo.push(D! < w.vf ? { cp: q.cp, d: q.d, vtLe: w.vf } : { cp: q.cp, d: q.d, vfGe: w.vt })
    }
    const perAsk = async (qs: { cp: string; d: number }[]) => {
      for (let i = 0; i < qs.length; i += PAR) {
        const chunk = qs.slice(i, i + PAR)
        const got = await Promise.all(chunk.map(q => lookup(date, v, q.cp, q.d)))
        chunk.forEach((q, j) => out.set(q.cp, got[j]))
      }
    }
    if (lens || !todo.length) { await perAsk(todo); return out }
    const room = walkOver() ? 0 : Math.max(0, LOOKUP_CAP - lookups)
    const take = todo.slice(0, room)
    if (todo.length > room) { capped = true; for (const q of todo.slice(room)) out.set(q.cp, null) }
    if (!take.length) return out
    const want = new Map(take.map(q => [`${q.d}\0${q.cp}`, q]))
    let got: Row[]
    try {
      got = (await readAsks(await fine(date), take.map(q => ({ depth: q.d, path: q.cp, ...(q.vtLe != null ? { vtLe: q.vtLe } : {}), ...(q.vfGe != null ? { vfGe: q.vfGe } : {}) })), r => want.has(`${r.depth}\0${r.path}`), { maxGroups: 120, stop: () => lookupsOff })).rows
    } catch (e) {
      if (!/too wide/.test(String((e as Error).message ?? e))) throw e
      tr?.('perask', take.length)
      await perAsk(take)
      return out
    }
    lookups += take.length
    const byPath = new Map<string, { all: Agg; mine: Agg }>()
    for (const r of got) {
      let e = byPath.get(r.path)
      if (!e) byPath.set(r.path, (e = { all: newAgg(), mine: newAgg() }))
      const cr = classRow(r, o.classes)
      merge(e.all, cr)
      if (ownerOk(r.usr, owner)) merge(e.mine, cr)
    }
    for (const q of take) {
      const e = byPath.get(q.cp)
      if (!e) { out.set(q.cp, null); continue }
      const a = net(v, q.cp, v.scoped(q.cp, e.all, e.mine))
      out.set(q.cp, a.b > 0 ? a : null)
    }
    return out
  }

  const rows: DiffRow[] = []
  let expansions = 0
  const rel = (p: string) => (path === '' ? p : p.slice(path.length + 1))
  const status = (a: Agg | null, b: Agg | null): DiffRow['s'] =>
    !a ? 'added' : !b ? 'removed' : Math.round(a.b) !== Math.round(b.b) || Math.round(a.o) !== Math.round(b.o) ? 'changed' : 'unchanged'
  const emit = (p: string, d: number, a: Agg | null, b: Agg | null, x: boolean, l?: 1 | 2) => {
    rows.push({
      p, d, k: a?.kind ?? b?.kind ?? 'dir', s: status(a, b),
      a: Math.round(a?.b ?? 0), b: Math.round(b?.b ?? 0), oa: Math.round(a?.o ?? 0), ob: Math.round(b?.o ?? 0),
      ...(x ? { x: true } : {}), ...(l ? { l } : {}),
    })
  }
  // Level by level, so a level's point lookups run in parallel (each is a
  // D1 span query + a row-group read; in series they dominated the walk).
  interface Item { p: string; d: number; a: Agg | null; b: Agg | null; l?: 1 | 2 }
  let level: Item[] = [{ p: path, d: dP, a: va?.rootAgg ?? null, b: vb?.rootAgg ?? null }]
  const PAR = 8
  while (level.length) {
    const next: Item[] = []
    // Expansion decisions and the names under each expanded node.
    const plans = level.map(it => {
      const { p, a, b } = it
      const ka = kidsA.get(p) ?? []
      const kb = kidsB.get(p) ?? []
      // Both sides present, something named under it on either side, and
      // the totals differ: the children carry the Δ. One-sided subtrees ride
      // whole on their own row; a byte-identical subtree is one unchanged
      // row (the renderer infers it as filler), not a walk of its skeleton.
      const same = !!(a && b && Math.round(a.b) === Math.round(b.b) && Math.round(a.o) === Math.round(b.o))
      // A one-sided node (a bucket the older scan never covered, a directory
      // that appeared or vanished) expands too: its children all read as
      // added / removed, but they still say WHAT arrived — a leaf tile
      // saying `84 Ti first scanned` says nothing about its shape.
      const expand = !!((a || b) && !same && (ka.length || kb.length)) && (o.depth == null || it.d - dP < o.depth)
      const names = expand ? [...new Set([...ka, ...kb])].sort() : []
      return { it, expand, names }
    })
    // The names one side lacks, looked up on that side — in parallel.
    const asks: { cp: string; d: number; side: 1 | 2; v: Read; date: string; other?: Agg }[] = []
    for (const { it, names } of plans) {
      for (const cp of names) {
        if (va && !va.kept.has(cp) && !va.complete?.(it.p)) asks.push({ cp, d: it.d + 1, side: 1, v: va, date: from, other: vb?.kept.get(cp) })
        if (vb && !vb.kept.has(cp) && !vb.complete?.(it.p)) asks.push({ cp, d: it.d + 1, side: 2, v: vb, date: to, other: va?.kept.get(cp) })
      }
    }
    const found = new Map<string, Agg | null>() // `${side}:${cp}`
    const sideAsks = (side: 1 | 2) => asks.filter(q => q.side === side)
    const level2 = Promise.all([
      va && sideAsks(1).length ? lookupMany(from, va, sideAsks(1)) : new Map<string, Agg | null>(),
      vb && sideAsks(2).length ? lookupMany(to, vb, sideAsks(2)) : new Map<string, Agg | null>(),
    ])
    // A filtered diff's level waits for its lookups until `FILTER_WALK_MS` is spent, no longer: the names
    // still unanswered read as one-sided, as past `LOOKUP_CAP` (`lookups_capped`, `lookups_late`).
    const left = query ? FILTER_WALK_MS - (performance.now() - walkStart) : Infinity
    let timer: ReturnType<typeof setTimeout> | null = null
    const none = [new Map<string, Agg | null>(), new Map<string, Agg | null>()] as const
    const [gotA, gotB] = left === Infinity ? await level2 : await Promise.race([
      level2,
      new Promise<typeof none>(r => { timer = setTimeout(() => { if (asks.length) { late = true; capped = true; lookupsOff = true } r(none) }, Math.max(0, left)) }),
    ])
    if (timer != null) clearTimeout(timer)
    level2.catch(() => {})
    for (const [cp, agg] of gotA) found.set(`1:${cp}`, agg)
    for (const [cp, agg] of gotB) found.set(`2:${cp}`, agg)
    for (const { it, expand, names } of plans) {
      const { p, d, a, b } = it
      if (p !== path) emit(rel(p), d - dP, a, b, expand, it.l)
      if (!expand) continue
      expansions++
      const sum = { a: newAgg(), b: newAgg() }
      for (const cp of names) {
        const ca = va?.kept.get(cp) ?? va?.exact?.get(cp) ?? found.get(`1:${cp}`) ?? null
        const cb = vb?.kept.get(cp) ?? vb?.exact?.get(cp) ?? found.get(`2:${cp}`) ?? null
        const lk: 1 | 2 | undefined = ca && !va!.kept.has(cp) && !va!.complete?.(p) ? 1 : cb && !vb!.kept.has(cp) && !vb!.complete?.(p) ? 2 : undefined
        if (ca) { sum.a.b += ca.b; sum.a.o += ca.o }
        if (cb) { sum.b.b += cb.b; sum.b.o += cb.o }
        next.push({ p: cp, d: d + 1, a: ca, b: cb, ...(lk ? { l: lk } : {}) })
      }
      // A one-sided parent has no residual on its missing side.
      const restA = newAgg()
      restA.b = Math.max(0, (a?.b ?? 0) - sum.a.b)
      restA.o = Math.max(0, (a?.o ?? 0) - sum.a.o)
      const restB = newAgg()
      restB.b = Math.max(0, (b?.b ?? 0) - sum.b.b)
      restB.o = Math.max(0, (b?.o ?? 0) - sum.b.o)
      if (restA.b > 0 || restB.b > 0) {
        const key = p === path ? '(other)' : `${rel(p)}/(other)`
        emit(key, d - dP + 1, restA.b > 0 ? restA : null, restB.b > 0 ? restB : null, false)
      }
    }
    level = next
  }

  // Cap like the batch walk did: every expanded ancestor (the skeleton) plus
  // the top-|Δ| changed frontier rows; unchanged frontier rows are inferred
  // as filler by the renderer.
  tr?.('walk', performance.now() - walkStart)
  const skeleton = rows.filter(r => r.x)
  const frontier = rows
    .filter(r => !r.x && r.s !== 'unchanged')
    .sort((r1, r2) => Math.abs(r2.b - r2.a) - Math.abs(r1.b - r1.a))
  return {
    rows: [...skeleton, ...frontier.slice(0, o.top)],
    ...totals,
    expansions,
    truncated: frontier.length > o.top,
    lookups,
    lookups_capped: capped,
    ...(late ? { lookups_late: true as const } : {}),
  }
}

/** The by-path sort a lens view reads on `date`: a version-1 scan's `user`;
 * a store generation's own `user` copy where it wrote one, else its `path`
 * filtered per row (a v1 `user` left on a store date never answers —
 * `tryOpen`). Subtree reads also try the user-first `bysize-user`
 * (`readSubtree`), which is what keeps a user's root view narrow. */
export async function lensSort(env: Env, date: string): Promise<string> {
  const p = await tryOpen(env, date, 'path')
  if (!(p && isStore(p))) return 'user'
  return (await tryOpen(env, date, 'user')) ? 'user' : 'path'
}

async function openFine(env: Env, date: string, sort: string, lens?: Lens): Promise<IndexHandle> {
  try {
    return await openIndex(env, date, sort)
  } catch (e) {
    if (lens && String((e as Error).message).includes('not synced')) throw new LensUnavailable(sort)
    throw e
  }
}

// --- the filter's cover: every match root, and the totals that collapse them (`cover.ts`) ----------------

/** Every match root under `o.path` on `o.date` (unscoped, net of nothing) with its bytes and objects — the
 *  whole list, where a view's `matched` is capped at `MATCH_LIST_CAP` — and why it may be incomplete:
 *  `coverage` (a budget-cut or thresholded search), `rollup` (a heavy literal under a heavy folder: the read
 *  knows per-child totals, not its roots). Null: no matches. Phase 1 only (`maxDepth: 0`): the static
 *  index's roots are held per isolate. */
export async function allMatchRoots(env: Env, o: { date: string; path: string; query: NamePred }): Promise<{ roots: { path: string; b: number; o: number }[]; coverage: Coverage; rollup: boolean } | null> {
  const cov: Coverage = {}
  const v = await readView(env, { date: o.date, path: o.path, query: o.query, w: 1280, h: 800, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, maxDepth: 0, firstPaint: true }, cov)
  if (!v) return v === null && (cov.partial || cov.approximate || cov.dirsOnly) ? { roots: [], coverage: cov, rollup: false } : null
  return { roots: (v.matched ?? []).slice().sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)), coverage: cov, rollup: !!v.rollup }
}

/** Row groups one cover lookup may read, and all of a cover's lookups together. */
export const COVER_LOOKUP_GROUPS = 60
export const COVER_TOTAL_GROUPS = 240

/** Totals and kinds of paths on `date` from the `path` sort (every owner slice summed), by point lookups:
 *  `'wide'` for a call that would read more than `budget.call` row groups (or than what's left), null once
 *  the calls together have read `budget.total`. */
export function pathTotals(env: Env, date: string, budget = { call: COVER_LOOKUP_GROUPS, total: COVER_TOTAL_GROUPS }): (paths: string[]) => Promise<Map<string, { b: number; o: number; kind: 'file' | 'dir' }> | null | 'wide'> {
  let h: Promise<IndexHandle> | undefined
  let used = 0
  return async paths => {
    if (used >= budget.total) return null
    h ??= openFine(env, date, 'path')
    const want = new Set(paths)
    let got: { rows: Row[]; groups: number }
    try {
      got = await readAsks(await h, paths.map(p => ({ depth: p.split('/').length, path: p })), r => want.has(r.path), { maxGroups: Math.min(budget.call, budget.total - used) })
    } catch (e) {
      // Too wide for this call: nothing was read (the plan stops before any group), so the budget stands.
      if (/too wide/.test(String((e as Error).message ?? e))) return 'wide'
      throw e
    }
    used += got.groups
    const out = new Map<string, { b: number; o: number; kind: 'file' | 'dir' }>()
    for (const r of got.rows) {
      const t = out.get(r.path)
      if (t) { t.b += r.size; t.o += r.n_files } else out.set(r.path, { b: r.size, o: r.n_files, kind: r.kind })
    }
    return out
  }
}
