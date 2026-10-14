/** The map's name filter from the static name index on R2 (specs/architecture/static-name-search.md): a
 *  single-literal `?f=` query's match roots, exact on every scan of the generation, without walking the
 *  per-scan path store for them (`FILTER_STATIC=1` with the `INDEX_R2` binding).
 *
 *  Why it is exact: the filter matches a path when its lowercase full path (`bucket/dir/…`) contains the
 *  literal `q`. A literal without `/` lies inside one segment, so a path matches iff some segment contains
 *  `q`, and the outermost matching paths under a view root `P` that `P` itself does not match (the match
 *  roots, `filter.ts` `matchRoots`) are exactly the paths under `P` whose lowercase name contains `q` and
 *  whose lowercase parent does not — the static index's first hits, buckets (depth 1, parent `''`)
 *  included. Each first hit is one version of one owner slice `(path, usr)` with its `size`/`n_files`, so a
 *  root's bytes, objects and per-owner split on scan `D` are the sum of its slices live on `D`.
 *
 *  Which queries: `staticLiteral` — one positive substring matcher without `/`, no other term, glob, regex
 *  or exclusion. Which literals: those of ≥ 3 characters whose suffix range the shard group index bounds by
 *  `maxRows` (V = 100K plus two row groups of slack, so every non-member and the lightest members) are read
 *  whole once per isolate (and once per colo through the Cache API); heavier ones, and every one- or
 *  two-character literal, ask the `heavy` source — the drilldown (`staticDrill.ts`, `FILTER_STATIC_HEAVY=1`):
 *  the match roots under the view path when they are few, else the path's per-child rollup, its drawn children nested (`view.ts` `rollupRead`) — and otherwise
 *  decline, so the caller keeps today's read.
 *
 *  Dates: an answer carries the scans it covers (`Found.scans`; absent = every scan of the store). The light
 *  index spans the base generation and its runs (one per scan); the drilldown the base and the runs whose
 *  `drill/` is live, up to the first run without one, so a heavy literal on a later scan is not covered (`covers`)
 *  and the view is refused (`scan-not-indexed`) — never the thresholded walk's "no matches". */
import type { QueryAst } from './queryAst.js'
import { shared } from './shared.js'
import { StaticCatalog } from './staticCatalog.js'
import { type CatalogLookup, Drill, DRILL_DIR, DrillSource, type Rollup, type RollupCell } from './staticDrill.js'
import { type Blobs, cacheIndexes, type FirstHits, type Hit, type Io, r2Blobs, scanAt, staticGen, staticPrefix } from './staticNames.js'
import { tiers } from './staticRuns.js'
import { ANCHOR_MIN, type FilterReject, type FilterRejectCode, reject } from './indexedOnly.js'
import { AnchoredSource, parseKey, termInPath, termKey } from './staticAnchors.js'
import { type HexRule, hexAffected } from './hexRuns.js'
import type { Matcher } from './queryAst.js'
import { compileQuery, type NamePred } from './pathQuery.js'

export type { Hit } from './staticNames.js'
export { type Rollup, rollupAt, rollupTotal } from './staticDrill.js'

/** The key of the literal a query is, when the static index can answer it exactly, else null: a plain literal
 *  (lowercase) itself, an anchored one (`^q`, `q$`, `^q$`; at least `ANCHOR_MIN` characters) `staticAnchors.ts`'s
 *  `termKey` (`/q`, `q/`, `/q/`). */
export function staticLiteral(ast: QueryAst | undefined): string | null {
  if (!ast || ast.neg.length || ast.alts.length !== 1 || ast.alts[0].length !== 1) return null
  const m = ast.alts[0][0]
  if (m.kind !== 'sub' || m.text.includes('/') || !m.text) return null
  const start = !!m.start, end = !!m.end
  if ((start || end) && [...m.text].length < (start && end ? ANCHOR_MIN.exact : start ? ANCHOR_MIN.start : ANCHOR_MIN.end)) return null
  return termKey({ text: m.text, start, end })
}

/** One or two characters: never a suffix-range read (the shards hold suffixes of ≥ 3); the heavy source's. */
export const shortLiteral = (key: string): boolean => [...key].length <= 2 && !parseKey(key).mode

/** What a source found for a literal under a path, any date: its match roots (`hits`: first hits, each a
 *  version of an owner slice), or — a heavy literal under a heavy directory — the path's per-child totals
 *  (`rollup`: kept children by name plus an exact remainder, no owners). `scans`: the scans the answer is
 *  exact on (absent: every scan of the store). */
export type Found =
  | { hits: Hit[]; rollup?: undefined; io: Record<string, unknown>; scans?: string[] }
  | { rollup: Rollup; hits?: undefined; io: Record<string, unknown>; scans?: string[] }

/** Whether `found` answers every one of `dates`: the explicit rule that keeps a heavy literal off the scans
 *  its drilldown does not cover (past the first run whose `drill/` is not live): `scan-not-indexed`. */
export const covers = (found: Found, dates: string[]): boolean => !found.scans || dates.every(d => found.scans!.includes(d))

/** Whether `found` is the drilldown's (`DrillSource`): a heavy literal's answer, exact on the drill's live tiers only. */
export const fromDrill = (found: Found | null): boolean => found?.io.from === 'drill'

/** A literal's match roots under a path, any date: what the filter needs from an index. `null` = this
 *  source can't answer the literal (the caller falls back). `under` = `''` (everything) or a path, whose
 *  strict descendants are wanted. */
/** `firstPaint`: a view's fast first paint asks — a source may answer coarser now (`^q`'s fleet root from its
 *  catalog while its range is not yet read), the full read following. */
export interface HitOpts {
  firstPaint?: boolean
  /** A nested read's budget (`view.ts` `nestRollup`): a source whose answer would be more than this many match
   *  roots — bounded so from its index before a read, or held with that many — declines (null) and sets `over`. */
  maxRows?: number
  over?: boolean
}

export interface HitSource {
  hits(key: string, under: string, opts?: HitOpts): Promise<Found | null>
  /** Why `hits` last declined `key`, when the source knows better than "not indexed" (an anchored prefix too
   *  common to read: `term-too-common`). */
  why?(key: string): FilterRejectCode | undefined
  /** Whether a heavy literal (short, or past the suffix bound) has a source (`FILTER_STATIC_HEAVY`'s
   *  drilldown): without one, `hits` declines it on every scan — the term, not the scan, is the reason. */
  readonly heavy?: boolean
  /** Whether `hits` last declined `key` as a heavy literal its heavy source declined too (a member the drill's
   *  newest live tier doesn't know yet, its stack cut at a run with no drill): no light answer either. */
  heavyDeclined?(key: string): boolean
}

/** The generation's scans (ids). `dirOnly`: those of them indexed from a dir-only (v1) source — held, but blind
 *  to every file name, so a literal's total there is not the scan's (`/api/series` leaves them as gaps). `hexRuns`:
 *  its hex-run rule (`hexRuns.ts`; absent or null = the full index). */
export interface StaticFilterStore { source: HitSource; scans: () => Promise<string[]>; dirOnly?: () => Promise<string[]>; gen: string; hexRuns?: () => Promise<HexRule | null> }

/** A suffix range read whole above this many rows (row-group granular) is declined: V = 100K (the
 *  catalog's membership bound, so every non-member fits) plus two 8K-row groups of slack. */
export const MAX_ROWS = 100_000 + 2 * 8192

/** Hits held per isolate, across literals (each ~150 B): the oldest literal is dropped past this. */
const HELD_HITS = 400_000

const under = (p: string, root: string): boolean => root === '' || p.startsWith(root + '/')

/** What `SuffixHits` reads: a literal's first hits, refused (null) above `maxRows`. `snapshot` names the index's
 *  current state (the runs' manifest, by its newest scan, `staticRuns.ts`) and the scans it covers: the hit lists
 *  span every scan, so they are held and cached per version. A read reports the stack it was read over (`version`,
 *  `scans`): a tier that fails mid-read cuts the stack, and that answer is exact on the remaining scans only. A
 *  single generation is `StaticNames` (no `snapshot`: 'base', every scan of the store). */
export interface HitReader {
  read(key: string, maxRows?: number): Promise<{ io: Io; fold: FirstHits | null; version?: string; scans?: string[] }>
  snapshot?(): Promise<{ version: string; scans: string[] }>
  defaultMaxRows?(key: string, baseMaxRows: number): Promise<number>
}

/** The light source: the literal's suffix range from the shards, folded to its first hits once (per
 *  isolate, and per colo through `cache`), then cut to `under`. Literals over `maxRows` go to `heavy`. */
export class SuffixHits implements HitSource {
  private held = new Map<string, Promise<{ hits: Hit[]; io: Record<string, unknown>; scans?: string[] } | null>>()
  private cut = new WeakMap<Hit[], Map<string, Hit[]>>()
  constructor(
    readonly names: HitReader,
    readonly opts: { maxRows?: number; heavy?: HitSource | null; catalog?: CatalogLookup | null; cache?: HitCache | null; waitMs?: number; anchored?: HitSource | null } = {},
  ) {}

  why(key: string): FilterRejectCode | undefined { return parseKey(key).mode ? this.opts.anchored?.why?.(key) : undefined }

  /** Every first hit of `key` (null: over `maxRows`, the heavy source's business), and the scans they are exact
   *  on (absent: every scan of the store). */
  async all(key: string): Promise<{ hits: Hit[]; io: Record<string, unknown>; scans?: string[] } | null> {
    const snap = await this.names.snapshot?.()
    const version = snap?.version ?? 'base'
    // The base generation's entries keep their keys; a day's runs version theirs.
    const vkey = version === 'base' ? key : `${key}@${version}`
    const p = shared(this.held, vkey, async () => {
      const cached = await this.opts.cache?.get(vkey)
      if (cached) return { hits: cached, io: { from: 'cache', version }, scans: snap?.scans }
      const maxRows = this.opts.maxRows ?? await this.names.defaultMaxRows?.(key, MAX_ROWS) ?? MAX_ROWS
      const { io, fold, version: read = 'base', scans } = await this.names.read(key, maxRows)
      if (!fold) return null
      // A stack cut mid-read (a broken tier) is another version's answer: never cached (or held) under this one.
      if (read === version) await this.opts.cache?.put(vkey, fold.hits)
      return { hits: fold.hits, io: { from: 'shards', version: read, shard: io.shard, groups: io.groups, bytes: io.bytes, rows_read: io.rows_read, tiers: io.tiers ?? 1, ms: io.ms }, scans }
    }, this.opts.waitMs ?? 20_000)
    // Bound what the isolate holds: drop the oldest literals past `HELD_HITS` hits; a cut read is dropped at once.
    void p.then(got => { if (got && got.io.version !== version) this.held.delete(vkey); return this.trim() }, () => {})
    return p
  }

  private async trim(): Promise<void> {
    let n = 0
    const keys = [...this.held.keys()].reverse()
    for (const k of keys) {
      const got = await this.held.get(k)?.catch(() => null)
      n += got?.hits.length ?? 0
      if (n > HELD_HITS && k !== keys[0]) this.held.delete(k)
    }
  }

  get heavy(): boolean { return !!this.opts.heavy }

  private declinedHeavy = new Set<string>()
  heavyDeclined(key: string): boolean { return this.declinedHeavy.has(key) }

  /** A literal the light reader declines: the heavy source's; without one, the catalog's buckets at the fleet
   *  root (`catalogRoot`), and nothing below it (`declined`: `term-too-common`). */
  private async heavyHits(key: string, root: string, opts?: HitOpts): Promise<Found | null> {
    if (!this.opts.heavy) return root === '' && this.opts.catalog ? catalogRoot(this.opts.catalog, key) : null
    const got = await this.opts.heavy.hits(key, root, opts)
    if (got) this.declinedHeavy.delete(key)
    else if (!opts?.over) {
      this.declinedHeavy.add(key)
      if (this.declinedHeavy.size > 1024) this.declinedHeavy.delete(this.declinedHeavy.values().next().value!)
    }
    return got
  }

  async hits(key: string, root: string, opts?: HitOpts): Promise<Found | null> {
    // Anchored keys (`^q`, `q$`, `^q$`): the anchored source's (`staticAnchors.ts`), else nobody's.
    if (parseKey(key).mode) return this.opts.anchored ? this.opts.anchored.hits(key, root, opts) : null
    if (shortLiteral(key)) return this.heavyHits(key, root, opts)
    const got = await this.all(key)
    if (!got) return this.heavyHits(key, root, opts)
    const scans = got.scans ? { scans: got.scans } : {}
    if (root === '') return { hits: got.hits, io: got.io, ...scans }
    // One array per (literal, root) while the literal is held: the view's per-isolate phase 1 is keyed by it.
    let byRoot = this.cut.get(got.hits)
    if (!byRoot) this.cut.set(got.hits, (byRoot = new Map()))
    let hits = byRoot.get(root)
    if (!hits) {
      byRoot.set(root, (hits = got.hits.filter(h => under(h.path, root))))
      if (byRoot.size > 32) byRoot.delete(byRoot.keys().next().value!)
    }
    return { hits, io: got.io, ...scans }
  }
}

/** A heavy literal's fleet root with no drilldown (`FILTER_STATIC_HEAVY` off): the catalog's per-bucket cells —
 *  `/api/name-summary`'s numbers — as a rollup of the buckets, exact in bytes and objects, its root rows unknown
 *  (`rows` null) and nothing below a bucket (`bucketsOnly`). A one- or two-character miss is zero everywhere (no
 *  cells); a longer non-member declines (null). `scans`: the catalog's stack, when it reports one. */
export async function catalogRoot(catalog: CatalogLookup, key: string): Promise<Found | null> {
  const got = await catalog.lookup(key)
  if (!got.member && !shortLiteral(key)) return null
  const cells: RollupCell[] = (got.member?.cells ?? []).map(x => ({ kind: 1, child: x.bucket, vf: Number(x.vf) * 1000, b: x.b, o: x.o }))
  const buckets = new Set(cells.map(x => x.child)).size
  return { rollup: { dir: '', kept: buckets, rows: null, children: buckets, cells, bucketsOnly: true }, io: { from: 'catalog' }, ...(got.scans ? { scans: got.scans } : {}) }
}

/** A literal's first hits across isolates (the colo's Cache API). */
export interface HitCache { get(key: string): Promise<Hit[] | null>; put(key: string, hits: Hit[]): Promise<void> }

/** Hits as columns of JSON (sizes as decimal strings: a bucket's bytes pass 2^53). */
export function cacheHits(cache: Cache, prefix: string, version = 'hits-v1'): HitCache {
  const url = (key: string) => `https://static-filter.invalid/${prefix}/${version}/${encodeURIComponent(key)}.json`
  return {
    async get(key) {
      const r = await cache.match(url(key))
      if (!r) return null
      const c = await r.json() as { path: string[]; depth: number[]; usr: string[]; vf: number[]; vt: number[]; size: string[]; n: string[] }
      return c.path.map((path, i) => ({ path, depth: c.depth[i], usr: c.usr[i], vf: c.vf[i], vt: c.vt[i], size: BigInt(c.size[i]), n: BigInt(c.n[i]) }))
    },
    async put(key, hits) {
      const c = { path: hits.map(h => h.path), depth: hits.map(h => h.depth), usr: hits.map(h => h.usr), vf: hits.map(h => h.vf), vt: hits.map(h => h.vt), size: hits.map(h => String(h.size)), n: hits.map(h => String(h.n)) }
      await cache.put(url(key), new Response(JSON.stringify(c), { headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=604800' } }))
    },
  }
}

export type StaticFilterEnv = { FILTER_STATIC?: string; FILTER_STATIC_HEAVY?: string; FILTER_STATIC_ANCHORS?: string; FILTER_INDEXED_ONLY?: string; INDEX_R2?: R2Bucket; STATIC_GEN?: string }

/** The drilldown over a bucket's generation: the base `drill/` and its `runs`' (`staticDrill.ts`), with the fleet
 *  root from `opts.catalog` (default: the base catalog); `cached`: its indexes in `cache` under `prefix` (the
 *  generation's `staticPrefix`). */
export function drillSource(blobs: Blobs, cached?: { cache: Cache; prefix: string }, opts: { runs?: () => Promise<string[]>; catalog?: CatalogLookup } = {}): DrillSource {
  const { cache, prefix } = cached ?? {}
  const pre = (dir: string | null) => `${prefix}/${dir ? `${dir}/` : ''}${DRILL_DIR}`
  const catalog = opts.catalog ?? new StaticCatalog(blobs, cache ? cacheIndexes(cache, prefix!, 'catalog-v1') : undefined)
  const drill = new Drill(blobs, catalog, {
    runs: opts.runs,
    cache: cache ? dir => ({ top: cacheIndexes(cache, pre(dir), 'top-v1'), aliases: cacheIndexes(cache, pre(dir), 'aliases-v2') }) : undefined,
  })
  return new DrillSource(drill, cache ? cacheHits(cache, pre(null), 'roots-v1') : undefined)
}

/** A test's store for an env object (in place of the R2 binding's): held on the env under a symbol, so the copies a
 *  request makes of it (`withStore`'s request, `withPathStore`, `slices`, …) carry it too. */
const INJECTED = Symbol('injected static filter store')
export const injectedStores = {
  set: (env: object, s: StaticFilterStore): void => { (env as { [INJECTED]?: StaticFilterStore })[INJECTED] = s },
  get: (env: object): StaticFilterStore | undefined => (env as { [INJECTED]?: StaticFilterStore })[INJECTED],
}

let held: { r2: R2Bucket; gen: string; store: StaticFilterStore } | undefined
/** The isolate's static filter store over the bound bucket; null when the static filter is off. A test
 *  injects its own (`injectedStores`). */
export function staticFilterStore(env: StaticFilterEnv): StaticFilterStore | null {
  const injected = injectedStores.get(env)
  if (injected) return injected
  if (env.FILTER_STATIC !== '1' || !env.INDEX_R2) return null
  const gen = staticGen(env), pre = staticPrefix(gen)
  if (held?.r2 !== env.INDEX_R2 || held.gen !== gen) {
    // The base generation plus its runs (`staticRuns.ts`): each tier's indexes cached under its own prefix.
    const tierPre = (dir: string | null) => dir ? `${pre}/${dir}` : pre
    const t = tiers(r2Blobs(env.INDEX_R2, pre), { indexCache: dir => cacheIndexes(caches.default, tierPre(dir)), catalogCache: dir => cacheIndexes(caches.default, tierPre(dir), 'catalog-v1') })
    // Heavy literals (`FILTER_STATIC_HEAVY=1`): the drilldown over the base and the runs (the fleet root from the tiers' catalog).
    const runs = async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : [])
    const heavy = env.FILTER_STATIC_HEAVY === '1' ? drillSource(r2Blobs(env.INDEX_R2, pre), { cache: caches.default, prefix: pre }, { runs, catalog: t.catalog }) : null
    // Without it, a heavy literal's fleet root from the catalog's buckets (`catalogRoot`).
    // Anchored terms (`^q`, `q$`, `^q$`; `staticAnchors.ts`): on unless `FILTER_STATIC_ANCHORS=0`; a generation
    // without `anchors/meta.json` declines them.
    const sub = (dir: string | null, s: string) => `${tierPre(dir)}/${s}`
    const anchored = env.FILTER_STATIC_ANCHORS === '0' ? null : new AnchoredSource(t.tiers, r2Blobs(env.INDEX_R2, pre), {
      cache: cacheHits(caches.default, pre, 'anchored-hits-v1'),
      caches: {
        index: (dir, s) => cacheIndexes(caches.default, sub(dir, s)),
        keys: (dir, s) => cacheIndexes(caches.default, sub(dir, s), 'keys-v1'),
        top: dir => cacheIndexes(caches.default, sub(dir, 'anchors'), 'top-v1'),
      },
    })
    held = { r2: env.INDEX_R2, gen, store: { source: new SuffixHits(t.names, { cache: cacheHits(caches.default, pre), heavy, catalog: heavy ? null : t.catalog, anchored }), scans: t.scans, dirOnly: t.dirOnly, gen: `${gen}${heavy ? '+drill' : ''}`, hexRuns: () => t.names.hexRuns() } }
  }
  return held.store
}

/** Whether `date` can be answered statically for `ast`: the literal, else null. */
export async function staticKey(s: StaticFilterStore | null, ast: QueryAst | undefined, dates: string[]): Promise<string | null> {
  const key = staticLiteral(ast)
  if (!s || !key) return null
  const have = await s.scans()
  return dates.every(d => have.includes(d)) ? key : null
}

/** Which of `dates` are dir-only (v1) scans of the store (`StaticFilterStore.dirOnly`): every source's answer
 *  there — the suffix shards', the anchored and drill readers', the catalog's (`catalogRoot`) — comes from an
 *  index that only ever saw folder names, so a literal's matches there are its folder-name matches: exact as
 *  such (a folder's bytes and objects are its whole subtree's), never a file's. */
export async function dirsOnlyOf(s: StaticFilterStore | null, dates: string[]): Promise<string[]> {
  const held = s?.dirOnly ? await s.dirOnly() : []
  return dates.filter(d => held.includes(d))
}

/** A literal compared across a dir-only scan and a full one (a diff over the v1 → v2 cutover): one side counts
 *  folder names only, the other files too, so every file match would read as added — `scan-dirs-only`, never a
 *  diff. Null: comparable (neither side dir-only, or both), not a static literal, or a view root the literal
 *  matches (the plain view, nothing searched). */
export async function dirsOnlySplit(s: StaticFilterStore | null, ast: QueryAst | undefined, path: string, dates: string[]): Promise<FilterReject | null> {
  const key = staticLiteral(ast)
  if (!s || !key || termInPath(key, path, s.hexRuns ? await s.hexRuns() : null)) return null
  const n = (await dirsOnlyOf(s, dates)).length
  return n && n < dates.length ? reject('scan-dirs-only') : null
}

/** The filter's bytes and objects under the hits' root on `date` (Σ live first hits), with `keep` an
 *  owner test on the slice's `usr` (null = unowned). */
export function liveTotal(hits: Hit[], date: string, keep: (usr: string | null) => boolean = () => true): { b: number; o: number; roots: number } {
  const D = scanAt(date)
  let b = 0n, o = 0n
  const roots = new Set<string>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt) || !keep(h.usr === '' ? null : h.usr)) continue
    b += h.size; o += h.n; roots.add(h.path)
  }
  return { b: Number(b), o: Number(o), roots: roots.size }
}

/** Bumped when a static response's shape changes (2: roots folded under the pixel budget, capped lists;
 *  3: heavy literals from the drilldown, rollup views; 4: bounded phase 2 and the tile budget; 5: a 1–2
 *  character literal's fleet-root `matchCount.n` counted from the roots index, not 0; 6: heavy literals on
 *  the drill runs' scans; 7: with no drilldown, a heavy literal's fleet root from the catalog's buckets and a
 *  `term-too-common` refusal below it, never the approximate walk; 8: anchored terms — `^q`, `q$` were literals before;
 *  9: a heavy `^q` — its fleet root from the starts-with catalog, scoped reads below, a 400K-row bound; 10: a
 *  series' uncovered and dir-only scans are gaps named in `unindexed`, indexed-only or not — never zeros;
 *  12: a dir-only scan's answer flagged `dirsOnly`, a diff across a dir-only and a full scan `scan-dirs-only`;
 *  13: the hex-run rule — `hexRuns` on hex-affected literals, `occurs` in the fallback and the anchored reader;
 *  14: an anchored term on a generation with no anchors build is `anchor-not-indexed`, never the approximate walk;
 *  15: `\^`, `\$`, `\\` escapes — `q\$` and `\^q` were anchored or `\`-holding literals before;
 *  16: a heavy literal on a scan its drilldown doesn't cover — past a run with no live `drill/` — is
 *  `scan-not-indexed`, never the walk's "no matches"; 17: any static literal on a scan the generation doesn't hold — newer
 *  than its newest (published, not yet appended) or before its base — or that its answer doesn't cover is `scan-not-indexed`
 *  on every deployment, indexed-only or not: the walk answered it before, missing small matches; 18: a heavy literal's
 *  rollup view nests — its drawn children answered in turn, to `FILTER_SUBDIV_LEVELS` below the view, in the view and
 *  both sides of a diff; 19: a budget-cut nest flags `budgetCut` for the client to re-ask — cached 18s from a deploy
 *  between the two could serve a cut tree unflagged). */
const RESPONSE_V = 19

/** The query's substring matchers (positive and negative): the literals the hex-run rule applies to. */
const subMatchers = (ast: QueryAst | undefined): Extract<Matcher, { kind: 'sub' }>[] =>
  ast ? [...ast.alts.flat(), ...ast.neg].filter((m): m is Extract<Matcher, { kind: 'sub' }> => m.kind === 'sub') : []
export const subTexts = (ast: QueryAst | undefined): string[] => subMatchers(ast).map(m => m.text)

/** The cache keys' static marker: the generation when the static filter would answer this query's literal, or
 *  when the query has any substring matcher (the generation's hex-run rule decides how the fallback matches it), so
 *  a response never outlives a switch of backend or generation; else ''. */
export function staticTag(env: StaticFilterEnv, query: { ast?: QueryAst } | undefined): string {
  const s = staticFilterStore(env)
  return s && (staticLiteral(query?.ast) || subTexts(query?.ast).length) ? `${s.gen}.${RESPONSE_V}` : ''
}

/** The deployment's hex-run rule: its static store's generation's (null: no static store, or a generation built
 *  without the rule). The filter's predicate then matches substrings by `occurs` under it (`pathQuery.ts`), so the
 *  static and the path-store answers for a literal agree. */
export async function staticHexRule(env: StaticFilterEnv): Promise<HexRule | null> {
  const s = staticFilterStore(env)
  return s?.hexRuns ? await s.hexRuns() : null
}

/** `query` recompiled under the deployment's hex-run rule (`staticHexRule`), with the rule: unchanged when there is
 *  none (or no query). */
export async function hexQuery(env: StaticFilterEnv, query: NamePred | undefined): Promise<{ query: NamePred | undefined; hexRuns: HexRule | null }> {
  const rule = query?.ast ? await staticHexRule(env) : null
  return { query: rule ? compileQuery(query!.ast!, { hexRuns: rule }) : query, hexRuns: rule }
}

/** A response's `hexRuns` note: the rule, when it is on and some substring of the query is hex-affected (its
 *  matches inside long hex ids aren't counted); else nothing. Never a refusal. */
export function hexNote(rule: HexRule | null, ast: QueryAst | undefined): { hexRuns?: HexRule } {
  // A start-anchored literal (`^q`, `^q$`) occurs at a segment's start, which the rule never drops (`segmentOccurs`).
  return rule && subMatchers(ast).some(m => !m.start && hexAffected(m.text, rule)) ? { hexRuns: { min: rule.min, tail: rule.tail } } : {}
}

/** Why a static read declined: the source's own reason when it has one (`term-too-common` for a too-common `^q`,
 *  `anchor-not-indexed` for an anchored term with no anchors build); a scan outside the generation (`skey` null), or a heavy literal past the
 *  drill base, is `scan-not-indexed`; a heavy literal with no heavy source (`FILTER_STATIC_HEAVY` off) below
 *  the fleet root (where the catalog answers, `catalogRoot`) is `term-too-common` — on every scan alike,
 *  indexed or not. */
export function declined(s: StaticFilterStore | null, skey: string | null, raw: Found | null): FilterReject {
  const why = s && skey && !raw ? s.source.why?.(skey) : undefined
  if (why) return reject(why)
  return s && skey && !raw && s.source.heavy === false && !parseKey(skey).mode ? reject('term-too-common') : reject('scan-not-indexed')
}

/** An indexed-only deployment's coverage test (`indexedOnly.ts`): `ast` (one literal, `rejectAst` passed) is
 *  answered statically under `path` on every one of `dates`, else the refusal `declined` names (or, dates
 *  across a dir-only and a full scan, `scan-dirs-only`: `dirsOnlySplit`); a dir-only scan alone passes, its
 *  view flagged `dirsOnly`. A view root
 *  the literal matches is the plain view (nothing to search). The answer is held per isolate, so the view's
 *  own read reuses it. */
export async function indexedGate(env: StaticFilterEnv, ast: QueryAst | undefined, path: string, dates: string[], opts?: HitOpts): Promise<FilterReject | null> {
  const key = staticLiteral(ast)
  if (!key) return reject('unsupported-terms')
  const s = staticFilterStore(env)
  if (termInPath(key, path, await staticHexRule(env))) return null
  const skey = s ? await staticKey(s, ast, dates) : null
  if (!skey) return declined(s, null, null)
  const split = await dirsOnlySplit(s, ast, path, dates)
  if (split) return split
  const found = await s!.source.hits(key, path, opts)
  return found && covers(found, dates) ? null : declined(s, skey, found)
}
