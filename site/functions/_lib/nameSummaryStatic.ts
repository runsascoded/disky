/** `/api/name-summary` and its registry from the static name index on R2 (`NAME_SUMMARY_STATIC=1` with the
 *  `INDEX_R2` binding; specs/architecture/static-name-search.md). Unset, both routes are off (`nameSummary.ts`).
 *
 *  - Registry: every scan in the generation's `scans.json`, the buckets from `STORE_BUCKETS`, the catalog's
 *    membership bound V from `catalog/meta.json`.
 *  - A literal of one or two characters: the catalog (`staticCatalog.ts`); a miss is zero everywhere.
 *  - Longer: the suffix shards' group index first (`staticNames.ts`). A range of ≤ V rows (row-group
 *    granular, an upper bound) cannot be a member: read it. Otherwise the catalog: a member answers from
 *    its cells; a miss has an exact range ≤ V, so the read is bounded (≤ V + 2 row groups).
 *  - A dir-only (v1) scan (`scans.json` `version: 1`): its totals are its folder-name matches (exact as such),
 *    flagged `dirs_only: true`; compared with a full scan (`from`), a 400 `scan-dirs-only`.
 *  - Bucket geometry: the answer is root and bucket totals only (no drill), so each bucket's `pre`/`post`
 *    is its ordinal position in `bucket_paths` (`ordinal` geometry). */
import { decodeScan, isScanId, resolveScan } from '../../src/scanSlug.js'
import { indexedScan, noScan } from './scanArg.js'
import { datedNameRequest, parseName, parseNameRegistry, STATIC_CATALOG_SOURCE, STATIC_SOURCE } from '../../src/nameModel.js'
import { HOT_SCOPE } from '../../src/hotModel.js'
import { type Env, json } from './auth.js'
import { REJECT_MESSAGES } from './indexedOnly.js'
import { catalogAnswer, type CatalogIo, type CatalogMeta, type Member } from './staticCatalog.js'
import { type Answer, type Blobs, cacheIndexes, type Io, r2Blobs, staticGen, staticPrefix, type Totals } from './staticNames.js'
import { tiers } from './staticRuns.js'
import { type HexRule, hexAffected } from './hexRuns.js'

export type StaticNameEnv = { NAME_SUMMARY_STATIC?: string; INDEX_R2?: R2Bucket; STATIC_GEN?: string; STATIC_MAX_ROWS?: string; STORE_BUCKETS?: string; STORE?: string; DB?: D1Database }
export const staticEnabled = (env: StaticNameEnv): boolean => env.NAME_SUMMARY_STATIC === '1' && !!env.INDEX_R2
export const privateHeaders = { 'cache-control': 'private, no-store' }

/** A non-member's static read above this many rows is refused (it should never exceed V + 2 row groups). */
const MAX_ROWS = 400_000
const CAPABILITIES = { bucket_drill: false, child_drill: false, fallback: false }
const validation = (gen: string) => ({
  description: `exact first-hit totals from the static name index (generation ${gen}: suffix postings and catalog, rebuilt from the scans and checked against brute force offline); no per-request source oracle`,
  source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false,
})

/** The generation's readers: suffix shards, catalog, `scans.json`'s scan ids (sorted), and a clock for the timings. */
/** The suffix reader (`StaticNames`, or `TieredNames` over the base and its runs). */
export interface NameReader {
  extent(key: string, io: Io): Promise<{ rows: number } | null>
  /** `scans`: the ones the answer is exact on (a tiered reader's stack, cut at a broken tier); absent, all. */
  answer(key: string, dates: string[], maxRows?: number): Promise<{ io: Io; answer: Answer | null; scans?: string[] }>
}
/** The catalog (`StaticCatalog`, or `TieredCatalog`). */
export interface CatalogReader { info(): Promise<CatalogMeta>; lookup(q: string): Promise<{ io: CatalogIo; member: Member | null; scans?: string[] }> }
/** `dirOnly`: the scans indexed from a dir-only (v1) source (`staticRuns.ts` `dirOnlyScans`): their totals are
 *  the literal's folder-name matches only. `hexRuns`: the generation's hex-run rule (absent or null: the full index). */
export interface Store { names: NameReader; catalog: CatalogReader; scans: () => Promise<string[]>; dirOnly?: () => Promise<string[]>; clock: () => Promise<number>; hexRuns?: () => Promise<HexRule | null> }
let held: { r2: R2Bucket; gen: string; store: Store } | undefined

// The Workers clock only advances across I/O; a cache miss pins "now" after CPU-bound work (decode).
const tick = async () => { await caches.default.match('https://static-names.invalid/tick'); return Date.now() }

/** The isolate's readers over the bound bucket's generation `gen` (`staticGen(env)`; indexes held across requests). */
export function store(r2: R2Bucket, gen: string): Store {
  if (held?.r2 !== r2 || held.gen !== gen) {
    const root = staticPrefix(gen), blobs = r2Blobs(r2, root)
    // The base generation plus its runs (`staticRuns.ts`), each tier's indexes cached under its own prefix.
    const pre = (dir: string | null) => dir ? `${root}/${dir}` : root
    const t = tiers(blobs, { indexCache: dir => cacheIndexes(caches.default, pre(dir)), catalogCache: dir => cacheIndexes(caches.default, pre(dir), 'catalog-v1'), clock: tick })
    held = { r2, gen, store: { names: t.names, catalog: t.catalog, scans: t.scans, dirOnly: t.dirOnly, clock: tick, hexRuns: () => t.names.hexRuns() } }
  }
  return held.store
}
/** The suffix reader alone (`/api/static-bench`). */
export const names = (r2: R2Bucket, gen: string): NameReader => store(r2, gen).names

/** `scans.json`'s scan ids, sorted (held once loaded). */
export function scanIds(blobs: Blobs): () => Promise<string[]> {
  let ids: Promise<string[]> | undefined
  return () => {
    ids ??= blobs.json<{ scans: { id: string }[] }>('scans.json').then(d => d.scans.map(s => s.id).sort())
    ids.catch(() => { ids = undefined })
    return ids
  }
}
const bucketPaths = (env: StaticNameEnv): string[] => {
  const paths = (env.STORE_BUCKETS ?? '').split(',').map(s => s.trim()).filter(Boolean).sort()
  if (!paths.length) throw new Error('static names: STORE_BUCKETS is unset')
  return paths
}
const logicalStore = (env: StaticNameEnv) => `${env.STORE ?? 'gcs'}_fleet`

/** The registry: which scans exist, the buckets, and the catalog's bound. */
export async function staticRegistryBody(env: StaticNameEnv, s: Store): Promise<unknown> {
  const [dates, meta] = await Promise.all([s.scans(), s.catalog.info()])
  const body = {
    schema: 'static-name-registry-v1', logical_store: logicalStore(env), generation: staticGen(env), max_rows: meta.membership.max_rows,
    bucket_paths: bucketPaths(env), dates, levels: 1, scope: HOT_SCOPE, capabilities: { ...CAPABILITIES },
  }
  parseNameRegistry(body) // the page's own contract check
  return body
}

export type Plan = 'catalog' | 'bounded-name-postings'
/** `scans`: the ones the answers are exact on, when a tiered read was cut at a broken tier (absent: all). */
export interface KeyAnswer { plan: Plan; answers: Record<string, Totals>; io: { suffix?: Io; catalog?: CatalogIo; ms: Record<string, number> }; scans?: string[] }

/** One literal's per-date bucket totals by the dispatch above. */
export async function answerKey(s: Store, key: string, days: string[], maxRows = MAX_ROWS): Promise<KeyAnswer> {
  const V = (await s.catalog.info()).membership.max_rows
  const ms: Record<string, number> = {}
  let t = await s.clock()
  const lap = async (name: string) => { const now = await s.clock(); ms[name] = (ms[name] ?? 0) + now - t; t = now }
  const fromCatalog = async (): Promise<KeyAnswer | { catalog: CatalogIo; scans?: string[] }> => {
    const { io, member, scans } = await s.catalog.lookup(key)
    await lap('catalog')
    const sc = scans ? { scans } : {}
    return member ? { plan: 'catalog', answers: catalogAnswer(member, days), io: { catalog: io, ms }, ...sc } : { catalog: io, ...sc }
  }
  if ([...key].length <= 2) {
    const c = await fromCatalog()
    return 'plan' in c ? c : { plan: 'catalog', answers: Object.fromEntries(days.map(d => [d, {}])), io: { catalog: c.catalog, ms }, ...(c.scans ? { scans: c.scans } : {}) }
  }
  const probe: Io = { shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }
  const ext = await s.names.extent(key, probe)
  await lap('extent')
  let catalog: CatalogIo | undefined, missed: string[] | undefined
  if (ext && ext.rows > V) {
    const c = await fromCatalog()
    if ('plan' in c) return c
    catalog = c.catalog
    missed = c.scans
  }
  const { io, answer, scans } = await s.names.answer(key, days, maxRows)
  await lap('suffix')
  if (!answer) throw new Error(`static names: ${JSON.stringify(key)} reads ${io.rows_read} rows, over ${maxRows}`)
  io.index = probe.index
  // A miss's membership was decided on the catalog's stack: the answer is exact where both stacks reach.
  const both = missed && scans ? scans.filter(d => missed!.includes(d)) : missed ?? scans
  return { plan: 'bounded-name-postings', answers: answer.answers, io: { suffix: io, ...(catalog ? { catalog } : {}), ms }, ...(both ? { scans: both } : {}) }
}

const safe = (v: bigint): number => { const n = Number(v); if (!Number.isSafeInteger(n)) throw new Error('static names: total exceeds 2^53'); return n }
const notIndexed = () => json({ error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }, 400, privateHeaders)
const dirsSplit = () => json({ error: `${REJECT_MESSAGES['scan-dirs-only']} This is not a zero-match result.`, code: 'scan-dirs-only' }, 400, privateHeaders)
const unavailable = () => json({ error: 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.' }, 503, { ...privateHeaders, 'retry-after': '1' })

/** `/api/name-summary` from R2: a 400 for a scan outside the generation, a 503 on any failure. */
export async function staticSummary(env: StaticNameEnv, params: URLSearchParams, s?: Store): Promise<Response> {
  const t0 = Date.now()
  try {
    const gen = staticGen(env)
    s ??= store(env.INDEX_R2!, gen)
    const asked = datedNameRequest(params), key = asked.name
    const [have, meta, rule] = await Promise.all([s.scans(), s.catalog.info(), s.hexRuns?.() ?? null])
    // A hex-affected literal on a generation with the hex-run rule: answered as always, and the body says that
    // matches inside long hex ids aren't counted (`hexRuns`, the page's note).
    const hex = rule && hexAffected(key, rule) ? { hexRuns: { min: rule.min, tail: rule.tail } } : {}
    // `date` / `from` name a scan of the STORE, resolved as every page and API does (`scanArg.ts`
    // `indexedScan`: a held id is itself, a slug the latest store scan it names; `2026-10-09` = that day's
    // latest scan when no scan is that id) — never the latest scan the static index happens to hold, which
    // would answer for another scan than the map's. A store scan the index doesn't cover yet is a 400
    // `scan-not-indexed`; a value naming no scan at all is the uniform 404.
    const resolve = async (raw: string): Promise<string | null> => {
      if (have.includes(raw)) return raw
      const prefix = decodeScan(raw)
      if (!prefix) return null
      // No store index to ask (no D1): the static index's own scans, an exact id as given.
      if (!env.DB) return resolveScan(raw, have) ?? (isScanId(raw) ? raw : null)
      const e = env as unknown as Env
      return (isScanId(raw) ? await indexedScan(e, raw, true) : null) ?? await indexedScan(e, prefix, false)
    }
    const date = await resolve(asked.date), from = asked.from === undefined ? undefined : await resolve(asked.from)
    for (const [k, raw, got] of [['date', asked.date, date], ['from', asked.from, from]] as const) {
      if (raw === undefined) continue
      if (!got) return noScan(k, raw)
      if (!have.includes(got)) return notIndexed()
    }
    if (from && date && from >= date) return json({ error: '`from` must be an earlier scan than `date`.' }, 400, privateHeaders)
    const request = { ...asked, date: date!, ...(from === undefined || from === null ? {} : { from }) }
    const days = from ? [from, date!] : [date!]
    // A dir-only (v1) scan's totals are its folder-name matches: flagged (`dirs_only`) alone, refused beside a full scan.
    const dirs = s.dirOnly ? await s.dirOnly() : []
    const dirsOnly = days.filter(d => dirs.includes(d))
    if (dirsOnly.length && dirsOnly.length < days.length) return dirsSplit()
    const { plan, answers, io, scans } = await answerKey(s, key, days, Number(env.STATIC_MAX_ROWS ?? MAX_ROWS))
    // A tier broke during the read: the index answers the scans before it only.
    if (scans && days.some(d => !scans.includes(d))) return notIndexed()
    const paths = bucketPaths(env), store_ = logicalStore(env)
    const sides = days.map(date => {
      const totals = answers[date]
      if (Object.keys(totals).some(b => !paths.includes(b))) throw new Error('static names: a bucket outside STORE_BUCKETS')
      const buckets = paths.map((path, i) => { const [b, o] = totals[path] ?? [0n, 0n]; return { path, pre: i + 1, post: i + 1, b: safe(b), o: safe(o) } })
      return {
        schema: 'dated-name-summary-v1', logical_store: store_, target: 'static_names', date, pattern: key, path: '', exact: true, incremental: false, levels: 1,
        scope: HOT_SCOPE, plan, source: plan === 'catalog' ? STATIC_CATALOG_SOURCE : STATIC_SOURCE,
        source_identity: { kind: 'static-names-v1', generation: gen, max_rows: meta.membership.max_rows },
        validation: validation(gen), capabilities: { ...CAPABILITIES },
        root: { b: buckets.reduce((a, x) => a + x.b, 0), o: buckets.reduce((a, x) => a + x.o, 0) }, buckets,
        ...(dirsOnly.includes(date) ? { dirs_only: true } : {}),
      }
    })
    let body: unknown = { ...sides[0], ...hex }
    if (request.from) {
      const [before, after] = sides
      const d = (a: { b: number; o: number }, b: { b: number; o: number }) => ({ b: b.b - a.b, o: b.o - a.o })
      body = {
        schema: 'dated-name-summary-diff-v1', logical_store: store_, from: request.from, date: request.date, pattern: key, path: '',
        exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, before, after, delta: d(before.root, after.root), capabilities: { ...CAPABILITIES },
        buckets: before.buckets.map((a, i) => { const b = after.buckets[i]; return { path: a.path, before: { pre: a.pre, post: a.post, b: a.b, o: a.o }, after: { pre: b.pre, post: b.post, b: b.b, o: b.o }, delta: d(a, b) } }),
        ...hex,
      }
    }
    parseName(body, request) // the page's own contract check, before it leaves the Worker
    const total = Date.now() - t0
    return json(body, 200, {
      ...privateHeaders, 'x-query-engine': 'name-summary-static', 'x-static-io': JSON.stringify({ gen, plan, ...io }),
      'server-timing': [...Object.entries(io.ms).map(([k, v]) => `${k};dur=${v}`), `static;dur=${total}`].join(', '),
    })
  } catch (error) {
    console.error('static name summary failed', error)
    return unavailable()
  }
}

/** `/api/name-summary-registry` from R2. */
export async function staticRegistry(env: StaticNameEnv, s?: Store): Promise<Response> {
  try {
    return json(await staticRegistryBody(env, s ?? store(env.INDEX_R2!, staticGen(env))), 200, { ...privateHeaders, 'x-query-engine': 'name-summary-registry-static' })
  } catch (error) {
    console.error('static name registry failed', error)
    return unavailable()
  }
}
