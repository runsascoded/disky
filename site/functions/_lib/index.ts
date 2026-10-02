/**
 * A scan's index tiers (`<dir>/path-index[-coarse<E>][-by-user].parquet`) as
 * a row-group-pruned range reader — shared by `/api/subtree` (pixel-budget
 * drill), `/api/diff`, `/api/series` and the owner totals (exact bytes per
 * live claim, `_lib/ownerTotals.ts`).
 *
 * Each file is sorted (depth, path) (or (usr, depth, path)): the descendants
 * of P at each depth are one contiguous run, so any prefix query is a few
 * row-group selections on the footer stats plus ranged reads of just those
 * groups.
 *
 * The footer stats live in D1 (specs/path-agnostic-serving.md §2.1):
 * `index_schema` is the per-(date, variant) **pointer** — the generation
 * `gen` and bucket dir `dir` of the file set a run published — and
 * `index_row_groups` holds every group's stats + compact (~250 B) metadata,
 * keyed by that gen. Row-group selection is a SQL query scoped to the
 * handle's gen, and we fetch metadata only for the groups a query actually
 * reads, so the ~5 MB thrift footer is never parsed on a cold isolate. A run
 * never overwrites a file the pointer names: it lands a new generation and
 * flips the pointer last (specs/view-serving.md, "Index rewrite vs D1
 * footer"). A pointer whose row groups retention retired (`index-gc -r`)
 * opens the tier's **cold footer** instead (`pq` mode): the same rows as a
 * small parquet beside the tier (`<tier>.groups.parquet`, 512 rows per
 * group, stats on the pruning columns), whose own footer is range-read and
 * cached; a query prunes its groups by those stats with the predicates it
 * would send D1 and decodes only the survivors (specs/path-store.md §1.6).
 * Only a generation without one opens the `.groups.json` blob (`blob` mode:
 * the whole document, fine for small deployments — 86 MB on gcs is not).
 * Parsing the tier parquet's own footer is never an option: a floor-free
 * tier's ~27k-group footer exceeds the Worker's memory.
 */
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type FileMetaData, parquetMetadata, parquetMetadataAsync, parquetRead, parquetReadObjects, type RowGroup } from 'hyparquet'
import type { Env } from './auth.js'
import { shared } from './shared.js'
import { d1Variant, isPrimary, PRIMARY_STORE, storeKey } from './stores.js'
import { compressors } from './zstd.js'

/** A leaf of the stored parquet schema (`index_schema.schema_json`). */
export interface SchemaElement { type: string; name: string; repetition_type: string; converted_type?: string }

export const BUCKET = 'oa-gcs-usage-dvx'

/** One index row, named by the layer-2's columns (specs/path-store.md §1.1) —
 * the shape both generations decode to (`toRow`). A version-2 store sort
 * carries these names on disk, objects and dirs alike; a version-1 index
 * (dir rows only, the wire names `b, o, wts, wb, c2..c4, a`) is mapped onto
 * them at decode: `kind` is always `dir`, and the structural counts and
 * stamps it never had are null. */
export interface Row {
  path: string
  depth: number
  usr: string | null
  kind: 'file' | 'dir'
  /** Bytes at or under the path (the wire's `b`). */
  size: number
  /** Descendant objects, 1 for an object (the wire's `o`). */
  n_files: number
  /** Direct children, objects + dirs; null on a v1 row. */
  n_children: number | null
  /** All descendants; null on a v1 row. */
  n_desc: number | null
  /** Latest stamp at or under the path, epoch seconds; null on a v1 row. */
  mtime: number | null
  /** Size-weighted mean stamp, epoch seconds; null where the source has none. */
  mtime_mean: number | null
  /** The bytes `mtime_mean` weighs (v2: `size` where the mean is set; v1: `wb`). */
  mtime_w: number
  /** Last-read epoch day (the wire's `a`); null = never read / not tracked. */
  last_read: number | null
  /** `sum_storage_class_id_<k>` — bytes in classes 2..4 (Standard = the rest). */
  cls2: number
  cls3: number
  cls4: number
  /** Bytes by age at the scan date, `AGE_COLS` order (<1d … ≥3y;
   *  specs/row-age-strata.md); null where the generation has no age columns. */
  ages: number[] | null
}

/** The store's bytes-by-age columns (`dt_cloud.index.AGE_COLS`). */
export const AGE_COLS = ['age_b0', 'age_b1', 'age_b2', 'age_b3', 'age_b4', 'age_b5', 'age_b6']

interface GroupSpan {
  rowStart: number
  rowEnd: number
  dMin: number
  dMax: number
  pMin: string
  pMax: string
  bMax: number
}

export type FileSlice = { byteLength: number; slice: (s: number, e?: number) => Promise<ArrayBuffer> }

/** Per-request timing sink: `(phase, ms)` accumulates into a `Server-Timing`
 * header (`/api/subtree`, `/api/diff`), so DevTools shows where a cold view
 * went — D1 span queries, group-metadata fetch, range fetches, decode. Handles
 * are memoized across requests, so a trace rides on a per-request copy
 * (`withTrace`), never on the shared handle. */
export type Trace = (name: string, ms: number, desc?: string) => void
export const withTrace = <H extends IndexHandle>(h: H, trace?: Trace): H => (trace ? { ...h, trace } : h)
const now = () => performance.now()

// D1-backed: metadata comes from index_schema/index_row_groups per query.
interface D1Handle {
  mode: 'd1'
  file: FileSlice
  env: Env
  date: string
  variant: string
  /** The generation the schema row pointed at when this handle opened; every
   * row-group query is scoped to it, so a flip mid-handle is invisible. */
  gen: string
  /** The generation's bucket dir (`index_schema.dir`): where its sidecars
   * live (the search index, `search.ts`). */
  dir: string
  schema: SchemaElement[]
  /** `index_schema.version`: 1 = the dir-only index (wire names), 2 = a
   * path-store sort (layer-2 names, objects as rows, a `bysize` sibling). */
  version: number
  /** The columns a shaped read decodes (null = every column): a store
   * generation projects to the `Row` fields, so a bridge generation's wire
   * aliases and `created` are never fetched. */
  columns: string[] | null
  trace?: Trace
  /** A coarse tier's absolute byte floor (every path with subtree bytes >= floor
   * is present); null for the floor-free tier. */
  floor: number | null
}

/** A store generation (`index_schema.version` ≥ 2): rows are every path,
 * objects included, and a `bysize` sort exists beside `path`. */
export const isStore = (h: IndexHandle): boolean => h.version >= 2

/** A user lens filter: `usr` column = `key`. On a version-1 scan it reads the
 * by-user variant (rows keyed by `usr` first); on a store generation it reads
 * the store's own sorts, whose groups mix users, filtered per row. */
export type Lens = { key: string }

/** A variant whose rows are sorted by `usr` first (`user`, `bysize-user`,
 * `coarse<E>-user`, store-prefixed or not): its group's path stats only hold
 * inside a single-user group, so a lens tests the user range first. Every
 * other sort keeps its path rect, and a lens is one more condition on it. */
export const lensSorted = (variant: string): boolean => {
  const v = variant.slice(variant.indexOf(':') + 1)
  return v === 'user' || v.endsWith('-user')
}
/** Blob-backed: the same stats + compact metadata D1 holds for a tier, as
 * one document beside its parquet (`<tier>.groups.json`, written by
 * `index-sync`), held in memory. What a scan whose row groups retention
 * retired from D1 (`index-gc -r`) opens: one ~10 MB fetch, cached at the
 * edge and per isolate, then the D1 handle's selection over an array. */
interface BlobHandle extends Omit<D1Handle, 'mode'> {
  mode: 'blob'
  groups: BlobGroup[]
}
interface BlobGroup extends Span {
  uMin: string | null
  uMax: string | null
  rgJson: string
}
/** Cold-footer-backed: the tier's footer rows as `<tier>.groups.parquet`
 * (written beside it by `index-sync` / `index-blob`), opened when the D1
 * pointer has no row groups for its generation. The handle holds that
 * file's own (small) footer — one entry per footer group, with the group's
 * byte range and its stats folded into the same bounds a tier group has —
 * and a query decodes only the footer groups whose bounds pass its span
 * predicate (`pqGroups`). */
interface PqHandle extends Omit<D1Handle, 'mode'> {
  mode: 'pq'
  footer: FooterIndex
}
/** One row group of a `.groups.parquet`: its rows (= tier groups
 * `[rgStart, rgEnd)`), byte range, hyparquet metadata, and the bounds its
 * column stats give (null where a stat is missing: always a candidate). */
interface FooterGroup {
  n: number
  rgStart: number
  rgEnd: number
  byteStart: number
  byteEnd: number
  meta: RowGroup
  bounds: { dMin: number; dMax: number; pMin: string; pMax: string; bMax: number; uMin: string | null; uMax: string | null } | null
}
interface FooterIndex {
  key: string
  metadata: FileMetaData
  groups: FooterGroup[]
}
export type IndexHandle = D1Handle | BlobHandle | PqHandle

export const num = (v: unknown): number => (typeof v === 'bigint' ? Number(v) : (v as number) ?? 0)
export const str = (v: unknown): string =>
  typeof v === 'string' ? v : v instanceof Uint8Array ? new TextDecoder().decode(v) : String(v ?? '')

/** The index-store creds: an r2/s3 deploy sets `STORE_*`; unset falls back to
 *  the GCS HMAC pair so gcs/cw are unchanged (specs/union-of-roots.md). */
export const storeCreds = (env: Env) => ({
  accessKeyId: env.STORE_ACCESS_KEY_ID ?? env.GCS_HMAC_KEY_ID,
  secretAccessKey: env.STORE_SECRET_ACCESS_KEY ?? env.GCS_HMAC_SECRET,
})

/** Whether the index store is configured — the readiness gate every
 *  index-serving endpoint checks before reading (replaces the direct
 *  `GCS_HMAC_*` check, so an r2 deploy passes on `STORE_*`). */
export const storeReady = (env: Env): boolean => {
  const { accessKeyId, secretAccessKey } = storeCreds(env)
  return !!(accessKeyId && secretAccessKey)
}

/** Where the index store lives: `STORE_ENDPOINT`/`STORE_BUCKET`/`STORE_REGION`
 *  point any S3-compatible store (R2: the account's S3 endpoint + `auto`);
 *  unset = the GCS defaults. One place, so every proxy resolves the same store. */
export const storeTarget = (env: Env) => ({
  endpoint: env.STORE_ENDPOINT ?? 'https://storage.googleapis.com',
  bucket: env.STORE_BUCKET ?? BUCKET,
  region: env.STORE_REGION ?? 'us-east1',
})

/** The URI scheme a store's endpoint speaks, for copy that names the store
 *  (`gs://` for GCS's S3-compatible API, `r2://` for an R2 account endpoint,
 *  `s3://` for anything else). */
export const storeScheme = (endpoint: string): 'gs' | 'r2' | 's3' =>
  /storage\.googleapis\.com/.test(endpoint) ? 'gs' : /\.r2\.cloudflarestorage\.com/.test(endpoint) ? 'r2' : 's3'

/** The store's read allow-list: `STORE_PREFIXES` (comma-separated) replaces
 *  `defaults` when set, so a deployment whose artifacts live under other
 *  prefixes (cw: `cw-l2/` tiers, `cw-sweep/` records) names them once in
 *  `[vars]`. Every proxy passes its own defaults, so unset keeps today's
 *  per-endpoint lists. */
export const storePrefixes = (env: Env, defaults: string[]): string[] =>
  env.STORE_PREFIXES
    ? env.STORE_PREFIXES.split(',').map(s => s.trim()).filter(Boolean)
    : defaults

export function makeStore(env: Env) {
  return S3Store({
    ...storeTarget(env),
    prefixes: storePrefixes(env, ['listing/', 'snapshots/']),
    ...storeCreds(env),
  })
}

/** Index variant → parquet key under the generation dir D1 points at
 * (`index_schema.dir`, e.g. `listing/<date>/index/<gen>`). Variants are
 * `<tier>[-<sort>]`: tier `''` (floor-free) or `coarse<E>`; sort `path`
 * (default) or `user`; a store generation adds the size-bucket sort
 * `bysize` (+ `bysize-user`), specs/path-store.md §1.2. Mirrors
 * `INDEX_VARIANTS` in the dt-cloud CLI (specs/view-serving.md §1). */
export function indexKey(dir: string, variant: string): string {
  // The age index is a standalone index (per-path created-day strata), not a
  // path-index tier, so it keeps its own base name (specs/done/age-index.md).
  if (variant === 'age') return `${dir}/age-index.parquet`
  // Phase B: one path-major pyramid tier per bin (`age-pyramid-<bin>`).
  const pm = /^age-pyramid-(\d+(?:min|h|d|mo|y))$/.exec(variant)
  if (pm) return `${dir}/age-pyramid-${pm[1]}.parquet`
  // The cross-scan over-time index — a standalone singleton, own base name
  // (specs/obs-axis-indexing.md Phase 1).
  if (variant === 'over-time') return `${dir}/over-time.parquet`
  const m = /^(?:(coarse\d+|bysize)(?:-(user))?|(path|user))$/.exec(variant)
  if (!m) throw new Error(`bad index variant '${variant}'`)
  const tier = m[1] ? `-${m[1]}` : ''
  const sort = m[2] ?? (m[3] === 'path' ? undefined : m[3])
  return `${dir}/path-index${tier}${sort ? `-by-${sort}` : ''}.parquet`
}

/** The size-bucket twin of a by-path sort (`path` → `bysize`, `user` →
 * `bysize-user`): the same rows, `(⌊log2 size⌋ desc, path)`. */
export const sizeVariant = (sort: string): string => (sort === 'path' ? 'bysize' : `bysize-${sort}`)

/** Where a scan's floor-free path index lives (the D1 pointer); null when
 * the scan was never synced. For the raw-parquet proxy and other readers
 * outside the D1 row-group path. */
export async function indexDir(env: Env, date: string, variant = 'path'): Promise<string | null> {
  if (!env.DB) return null
  const r = await schemaRow<{ dir: string | null }>(env, 'dir', date, variant)
  return r?.dir ?? null
}

/** One `index_schema` pointer row of the env's store. The primary's query is
 * exactly the pre-stores one (runs on an un-migrated D1); a secondary store's
 * names `store` and its namespaced variant (`d1Variant`). */
export function schemaRow<T>(env: Env, cols: string, date: string, variant: string): Promise<T | null> {
  return isPrimary(env)
    ? env.DB!.prepare(`SELECT ${cols} FROM index_schema WHERE date = ? AND variant = ?`).bind(date, variant).first<T>()
    : env.DB!.prepare(`SELECT ${cols} FROM index_schema WHERE store = ? AND date = ? AND variant = ?`).bind(storeKey(env), date, d1Variant(env, variant)).first<T>()
}

/** The `path` pointer's generation for each of `dates`, folded into one short
 * token (FNV-1a over `date=gen` in date order; '' without a D1). An edge-cache
 * key carries it, so re-syncing a date (a new generation, e.g. a store
 * generation over an earlier v1 one) is a new key instead of serving views
 * cached from the old generation for the cache's whole TTL. One query per 90
 * dates (D1's bind limit is 100). */
export async function pathGens(env: Env, dates: string[]): Promise<string> {
  if (!env.DB || !dates.length) return ''
  const gens = new Map<string, string>()
  const uniq = [...new Set(dates)]
  for (let i = 0; i < uniq.length; i += 90) {
    const chunk = uniq.slice(i, i + 90)
    const marks = chunk.map(() => '?').join(', ')
    const { results } = isPrimary(env)
      ? await env.DB.prepare(`SELECT date, gen FROM index_schema WHERE variant = 'path' AND date IN (${marks})`).bind(...chunk).all<{ date: string; gen: string | null }>()
      : await env.DB.prepare(`SELECT date, gen FROM index_schema WHERE store = ? AND variant = ? AND date IN (${marks})`).bind(storeKey(env), d1Variant(env, 'path'), ...chunk).all<{ date: string; gen: string | null }>()
    for (const r of results) gens.set(r.date, r.gen ?? '')
  }
  let h = 0x811c9dc5
  for (const d of [...uniq].sort()) {
    for (const ch of `${d}=${gens.get(d) ?? ''};`) h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193)
  }
  return (h >>> 0).toString(36)
}

/** Every scan of the env's store with a synced floor-free (`path`) index —
 * the primary's query as it always was, a secondary store's scoped. */
export function pathScans(env: Env, order: boolean): Promise<{ results: { date: string }[] }> {
  const by = order ? ' ORDER BY date' : ''
  return isPrimary(env)
    ? env.DB!.prepare(`SELECT DISTINCT date FROM index_schema WHERE variant = 'path'${by}`).all<{ date: string }>()
    : env.DB!.prepare(`SELECT DISTINCT date FROM index_schema WHERE store = ? AND variant = ?${by}`).bind(storeKey(env), d1Variant(env, 'path')).all<{ date: string }>()
}

function fileFor(env: Env, dir: string, variant: string): FileSlice {
  const store = makeStore(env)
  const key = indexKey(dir, variant)
  let size: Promise<number> | null = null
  const byteLengthP = () => (size ??= store.get(key, { offset: 0, length: 1 }).then(r => {
    if (!r.totalSize) throw new Error('index size unknown (no Content-Range)')
    return r.totalSize
  }))
  return {
    // byteLength is never needed (row-group offsets are absolute); hyparquet
    // still expects the field, so it stays a lazy zero.
    get byteLength() { return 0 },
    slice: async (s: number, e?: number) => {
      const end = e ?? (await byteLengthP())
      const r = await store.get(key, { offset: s, length: end - s })
      return r.bytes.buffer.slice(r.bytes.byteOffset, r.bytes.byteOffset + r.bytes.byteLength) as ArrayBuffer
    },
  }
}

// Per-isolate cache of the pointer read (one D1 row). A generation is
// immutable, but the pointer can flip (a REPROC), so entries expire: after
// HANDLE_TTL a handle re-reads the schema row and follows the new generation.
// `index-gc` runs at the end of the job, well past the TTL, so a live handle
// never outlives its generation's rows.
const HANDLE_TTL = 60_000
const handles = new Map<string, Promise<IndexHandle>>()
const handleAt = new Map<string, number>()

export async function openIndex(env: Env, date: string, variant = 'path'): Promise<IndexHandle> {
  const ck = `${storeKey(env)}:${date}:${variant}`
  const at = handleAt.get(ck)
  if (at == null || Date.now() - at >= HANDLE_TTL) {
    handles.delete(ck)
    handleAt.set(ck, Date.now())
  }
  return shared(handles, ck, async (): Promise<IndexHandle> => {
    if (!env.DB) throw new Error('index reader not configured (DB)')
    const s = await schemaRow<{ version: number; schema_json: string; floor_bytes: number | null; gen: string | null; dir: string | null }>(env, 'version, schema_json, floor_bytes, gen, dir', date, variant)
    if (!s || !s.gen || !s.dir) throw new Error(`index variant '${variant}' not synced for ${date}`)
    // A pointer whose row groups were retired (`index-gc -r`) still names
    // the generation dir: open the tier's cold footer there, else (a
    // generation from before it was written) the group-manifest blob.
    const any = await env.DB.prepare('SELECT 1 AS x FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? LIMIT 1').bind(date, d1Variant(env, variant), s.gen).first<{ x: number }>()
    const schema = JSON.parse(s.schema_json) as SchemaElement[]
    const base = { file: fileFor(env, s.dir, variant), env, date, variant, gen: s.gen, dir: s.dir, schema, version: s.version, columns: rowColumns(s.version, schema), floor: s.floor_bytes == null ? null : num(s.floor_bytes) }
    if (any) return { mode: 'd1', ...base }
    let footer: FooterIndex
    try {
      footer = await openFooter(env, footerKey(s.dir, variant))
    } catch (e) {
      if ((e as Error).name !== 'NotFoundError') throw e
      return openBlob(env, date, variant, s.gen, s.dir)
    }
    return { mode: 'pq', ...base, footer }
  }, 30_000) // a blob open is a ~15 MB fetch + parse
}

/** The physical columns a `Row` decodes from, per generation — the wire
 * names on a v1 index, the layer-2's on a store sort (§1.1). `usr` and the
 * class pivots only where the file has them (cw has neither). */
export const V1_ROW_COLUMNS = ['path', 'depth', 'usr', 'b', 'o', 'wts', 'wb', 'c2', 'c3', 'c4', 'a']
export const V2_ROW_COLUMNS = ['path', 'depth', 'usr', 'kind', 'size', 'n_files', 'n_children', 'n_desc', 'mtime', 'mtime_mean', 'last_read', 'sum_storage_class_id_2', 'sum_storage_class_id_3', 'sum_storage_class_id_4', ...AGE_COLS]

/** What a shaped read projects: a v1 index reads every column (its columns
 * are the row); a store sort reads the `Row` columns it has, so a bridge
 * generation's wire aliases and `created` are never fetched or decoded. */
export function rowColumns(version: number, schema: SchemaElement[]): string[] | null {
  if (version < 2) return null
  const have = new Set(schema.slice(1).map(l => l.name))
  return V2_ROW_COLUMNS.filter(c => have.has(c))
}

/** The columns a caller's projection must name on this handle's generation,
 * for reads that pick a subset (the owner totals): the `Row` field → column. */
export function columnsFor(h: IndexHandle, fields: (keyof Row)[]): string[] {
  const v2: Partial<Record<keyof Row, string[]>> = { size: ['size'], n_files: ['n_files'], last_read: ['last_read'], cls2: ['sum_storage_class_id_2'], cls3: ['sum_storage_class_id_3'], cls4: ['sum_storage_class_id_4'], mtime_mean: ['mtime_mean', 'size'], mtime_w: ['mtime_mean', 'size'] }
  const v1: Partial<Record<keyof Row, string[]>> = { size: ['b'], n_files: ['o'], last_read: ['a'], cls2: ['c2'], cls3: ['c3'], cls4: ['c4'], mtime_mean: ['wts', 'wb'], mtime_w: ['wb'] }
  const map = isStore(h) ? v2 : v1
  const have = new Set(h.schema.slice(1).map(l => l.name))
  const out = new Set<string>()
  for (const f of fields) for (const c of map[f] ?? [f]) if (have.has(c)) out.add(c)
  return [...out]
}

/** The blob's on-disk shape (index_footer.py `groups_blob`): groups are
 * `index_row_groups` columns in order, `rg_json` as the string `reviveRowGroup`
 * takes. */
interface GroupsBlob {
  v: number
  version: number
  schema: SchemaElement[]
  floor_bytes: number | null
  groups: [number, number, number, string, string, number, string | null, string | null, number, number, string][]
}
const BLOB_CACHE_TTL = 86_400 // a generation dir is immutable

export const blobKey = (dir: string, variant: string): string => indexKey(dir, variant).replace(/\.parquet$/, '.groups.json')

async function openBlob(env: Env, date: string, variant: string, gen: string, dir: string): Promise<BlobHandle> {
  const key = blobKey(dir, variant)
  const cache = (caches as unknown as { default: Cache }).default
  // A secondary store's bucket may hold the same key: its entries get their own segment.
  const st = storeKey(env)
  const ck = new Request(`https://index-blob.cache/${st === PRIMARY_STORE ? '' : `@${st}/`}${key}`)
  let text: string
  const hit = await cache.match(ck)
  if (hit) text = await hit.text()
  else {
    let got: { bytes: Uint8Array }
    try {
      got = await makeStore(env).get(key)
    } catch (e) {
      // No rows and no blob: the tier is unreachable — the "not synced"
      // shape, which `tryOpen` folds and the floor-free open reports.
      throw new Error(`index variant '${variant}' not synced for ${date} (retired from D1; no ${key}: ${(e as Error).message})`)
    }
    text = new TextDecoder().decode(got.bytes)
    await cache.put(ck, new Response(text, { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
  }
  const blob = JSON.parse(text) as GroupsBlob
  if (blob.v !== 1) throw new Error(`${key}: unknown blob version ${blob.v}`)
  const groups: BlobGroup[] = blob.groups.map(([rg, dMin, dMax, pMin, pMax, bMax, uMin, uMax, rowStart, rowEnd, rgJson]) => ({ rg, dMin, dMax, pMin, pMax, bMax, uMin, uMax, rowStart, rowEnd, rgJson }))
  return { mode: 'blob', file: fileFor(env, dir, variant), env, date, variant, gen, dir, schema: blob.schema, version: blob.version, columns: rowColumns(blob.version, blob.schema), floor: blob.floor_bytes == null ? null : num(blob.floor_bytes), groups }
}

// --- the cold footer tier: `<tier>.groups.parquet` (`pq` mode) ---------------

/** The cold footer beside a tier (`disk_tree.find.groups.groups_parquet_path`). */
export const footerKey = (dir: string, variant: string): string => indexKey(dir, variant).replace(/\.parquet$/, '.groups.parquet')

/** The columns a footer row decodes (the writer's `FOOTER_COLS`, minus `b_min`:
 * no span predicate reads it yet). */
const FOOTER_COLS = ['rg', 'd_min', 'd_max', 'p_min', 'p_max', 'b_max', 'u_min', 'u_max', 'row_start', 'row_end', 'rg_json']

/** The first tail read of a `.groups.parquet`: its whole footer in one
 * request up to ~170 footer groups (~1.5 KB of thrift each, measured on cw's
 * `bysize`: 14 groups → 20.8 KB) — a gcs sort at 32K-row tier groups has
 * ~47; a larger footer costs one more request. */
const FOOTER_TAIL = 1 << 18

const colo = (): Cache => (caches as unknown as { default: Cache }).default
/** A colo-cache key for part of a store object — a secondary store's under its own segment. */
const coloKey = (env: Env, key: string, part: string): Request => {
  const st = storeKey(env)
  return new Request(`https://index-footer.cache/${st === PRIMARY_STORE ? '' : `@${st}/`}${key}?${part}`)
}
const toBuffer = (b: Uint8Array): ArrayBuffer => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer

/** Bytes `[start, end)` of a store object through the colo cache (a
 * generation dir is immutable, so a range is too). */
export async function cachedRange(env: Env, key: string, start: number, end: number): Promise<ArrayBuffer> {
  const ck = coloKey(env, key, `r=${start}-${end}`)
  const hit = await colo().match(ck)
  if (hit) return hit.arrayBuffer()
  const buf = toBuffer((await makeStore(env).get(key, { offset: start, length: end - start })).bytes)
  await colo().put(ck, new Response(buf, { headers: { 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
  return buf
}

/** A small parquet's footer bytes (metadata + the 8-byte trailer), colo-
 * cached: one tail read of `FOOTER_TAIL` bytes, one more when the footer is
 * longer. Throws the store's `NotFoundError` when the file is absent. */
export async function readFooterBytes(env: Env, key: string): Promise<ArrayBuffer> {
  const ck = coloKey(env, key, 'footer')
  let buf: ArrayBuffer
  const hit = await colo().match(ck)
  if (hit) buf = await hit.arrayBuffer()
  else {
    const store = makeStore(env)
    const size = (await store.get(key, { offset: 0, length: 1 })).totalSize
    if (size == null || !(size >= 12)) throw new Error(`${key}: size unknown or too small (${size})`)
    const at = Math.max(0, size - FOOTER_TAIL)
    let tail = new Uint8Array(toBuffer((await store.get(key, { offset: at, length: size - at })).bytes))
    const need = new DataView(tail.buffer).getUint32(tail.byteLength - 8, true) + 8
    if (need > size) throw new Error(`${key}: footer length ${need} exceeds the file (${size})`)
    if (need > tail.byteLength) {
      const head = (await store.get(key, { offset: size - need, length: need - tail.byteLength })).bytes
      const all = new Uint8Array(need)
      all.set(head, 0)
      all.set(tail, head.byteLength)
      tail = all
    }
    buf = toBuffer(tail.subarray(tail.byteLength - need))
    await colo().put(ck, new Response(buf, { headers: { 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
  }
  return buf
}

/** A `.groups.parquet`'s footer (`readFooterBytes`) parsed into one
 * `FooterGroup` per row group. Throws the store's `NotFoundError` when the
 * file is absent (the caller falls back to the `.groups.json` blob). */
async function openFooter(env: Env, key: string): Promise<FooterIndex> {
  const metadata = parquetMetadata(await readFooterBytes(env, key))
  const kv = new Map((metadata.key_value_metadata ?? []).map(e => [e.key, e.value]))
  if (kv.get('groups_v') !== '1') throw new Error(`${key}: not a v1 groups parquet (groups_v=${kv.get('groups_v')})`)
  let rg = 0
  const groups = metadata.row_groups.map((meta, n): FooterGroup => {
    const cols = new Map(meta.columns.map(c => [c.meta_data!.path_in_schema[0], c.meta_data!]))
    let byteStart = Infinity
    let byteEnd = 0
    for (const c of cols.values()) {
      const data = Number(c.data_page_offset)
      const dict = c.dictionary_page_offset == null ? data : Number(c.dictionary_page_offset)
      const start = dict > 0 ? Math.min(dict, data) : data
      byteStart = Math.min(byteStart, start)
      byteEnd = Math.max(byteEnd, start + Number(c.total_compressed_size))
    }
    const stat = (col: string) => cols.get(col)?.statistics
    const lo = (col: string) => { const s = stat(col); return s?.min_value ?? s?.min }
    const hi = (col: string) => { const s = stat(col); return s?.max_value ?? s?.max }
    const req = [lo('d_min'), hi('d_max'), lo('p_min'), hi('p_max'), hi('b_max')]
    // `u_*` are nullable: a group with stats but no min/max is all-NULL (no
    // lens matches it); a group without stats at all is unknown — as is one
    // missing any other bound — and always a candidate.
    const uKnown = stat('u_min') != null && stat('u_max') != null
    const bounds = req.some(v => v == null) || !uKnown
      ? null
      : { dMin: num(req[0]), dMax: num(req[1]), pMin: str(req[2]), pMax: str(req[3]), bMax: num(req[4]), uMin: lo('u_min') == null ? null : str(lo('u_min')), uMax: hi('u_max') == null ? null : str(hi('u_max')) }
    const rows = Number(meta.num_rows)
    const g = { n, rgStart: rg, rgEnd: rg + rows, byteStart, byteEnd, meta, bounds }
    rg += rows
    return g
  })
  return { key, metadata, groups }
}

/** Decode one footer group into the blob handle's group shape — cached per
 * isolate beside the tier groups, `(store, date, variant, gen, fg:<n>)`. */
async function readFooterGroup(h: PqHandle, fg: FooterGroup): Promise<BlobGroup[]> {
  const k = `${storeKey(h.env)}|${h.date}|${h.variant}|${h.gen}|fg:${fg.n}`
  const hit = cacheGet<BlobGroup>(k)
  if (hit) return hit
  const t0 = now()
  const buf = await cachedRange(h.env, h.footer.key, fg.byteStart, fg.byteEnd)
  const file: FileSlice = { byteLength: fg.byteEnd, slice: async (s, e) => buf.slice(s - fg.byteStart, (e ?? fg.byteEnd) - fg.byteStart) }
  const metadata = { ...h.footer.metadata, row_groups: [fg.meta], num_rows: fg.meta.num_rows }
  const rows = (await parquetReadObjects({ file, metadata, columns: FOOTER_COLS, compressors })) as Record<string, unknown>[]
  const out = rows.map((r): BlobGroup => ({
    rg: num(r.rg), dMin: num(r.d_min), dMax: num(r.d_max), pMin: str(r.p_min), pMax: str(r.p_max), bMax: num(r.b_max),
    uMin: r.u_min == null ? null : str(r.u_min), uMax: r.u_max == null ? null : str(r.u_max),
    rowStart: num(r.row_start), rowEnd: num(r.row_end), rgJson: str(r.rg_json),
  }))
  h.trace?.('footer', now() - t0, h.variant)
  cachePut(k, out, out.reduce((n, g) => n + 96 + 2 * (g.pMin.length + g.pMax.length + g.rgJson.length), 0))
  return out
}

/** The tier groups of a `pq` handle that `pass` (the span predicate)
 * accepts: footer groups whose bounds fail it are never fetched or decoded.
 * Sound because each predicate is monotone in the bounds — a footer group's
 * bounds contain every row's — and `groupMatches` / `groupMatchesSize`
 * applied to the bounds is exactly that relaxation (a depth-spanning range
 * skips the path test, a mixed-user one the keyed rect). */
async function pqGroups(h: PqHandle, pass: (g: NonNullable<FooterGroup['bounds']>) => boolean): Promise<BlobGroup[]> {
  const sel = h.footer.groups.filter(fg => !fg.bounds || pass(fg.bounds))
  h.trace?.('fgroups', sel.length)
  const all = (await mapLimit(sel, GROUP_READS, fg => readFooterGroup(h, fg))).flat()
  return all.filter(pass)
}

// --- shared row shaping ------------------------------------------------------

/** A v1 index row (the wire names; dirs only) as a `Row`. */
const toRowV1 = (r: Record<string, unknown>): Row => {
  const wb = num(r.wb)
  return {
    path: str(r.path),
    depth: num(r.depth),
    usr: r.usr == null ? null : str(r.usr),
    kind: 'dir',
    size: num(r.b),
    n_files: num(r.o),
    n_children: null,
    n_desc: null,
    mtime: null,
    mtime_mean: wb > 0 ? num(r.wts) / wb : null,
    mtime_w: wb,
    last_read: r.a == null ? null : num(r.a),
    cls2: num(r.c2),
    cls3: num(r.c3),
    cls4: num(r.c4),
    ages: null,
  }
}

/** A store-sort row (the layer-2 names, §1.1; `kind` on every row) as a `Row`. */
const toRowV2 = (r: Record<string, unknown>): Row => {
  const size = num(r.size)
  const mean = r.mtime_mean == null ? null : num(r.mtime_mean)
  return {
    path: str(r.path),
    depth: num(r.depth),
    usr: r.usr == null ? null : str(r.usr),
    kind: str(r.kind) === 'file' ? 'file' : 'dir',
    size,
    n_files: num(r.n_files),
    n_children: r.n_children == null ? null : num(r.n_children),
    n_desc: r.n_desc == null ? null : num(r.n_desc),
    mtime: r.mtime == null ? null : num(r.mtime),
    mtime_mean: mean,
    mtime_w: mean == null ? 0 : size,
    last_read: r.last_read == null ? null : num(r.last_read),
    cls2: num(r.sum_storage_class_id_2),
    cls3: num(r.sum_storage_class_id_3),
    cls4: num(r.sum_storage_class_id_4),
    ages: r.age_b0 == null ? null : AGE_COLS.map(c => num(r[c])),
  }
}

/** The decoder for a handle's generation. */
export const toRow = (h: { version: number }): ((r: Record<string, unknown>) => Row) => (h.version >= 2 ? toRowV2 : toRowV1)

// --- D1 metadata: revive stored RowGroup JSON into hyparquet's shape ---------

/** The compact form `index-sync` stores (index_footer.py): `[num_rows, codec,
 * [[data_page_offset, total_compressed_size, dictionary_page_offset|0], …]]`,
 * one triple per leaf column in schema order — the only column-chunk fields
 * hyparquet reads (plus `type`/`path_in_schema`, taken from the schema). */
type CompactGroup = [number, string, [number, number, number][]]

export function reviveRowGroup(json: string, schema: SchemaElement[]): Record<string, unknown> {
  const [numRows, codec, cols] = JSON.parse(json) as CompactGroup
  const leaves = schema.slice(1) // [0] is the root element
  if (cols.length !== leaves.length) throw new Error(`row group has ${cols.length} columns, schema ${leaves.length}`)
  return {
    num_rows: BigInt(numRows),
    columns: cols.map(([dpo, size, dict], i) => ({
      meta_data: {
        type: leaves[i].type,
        path_in_schema: [leaves[i].name],
        codec,
        data_page_offset: BigInt(dpo),
        total_compressed_size: BigInt(size),
        ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}),
      },
    })),
  }
}

/** The byte span `[start, end)` of a row group's column chunks — the
 * `columns` it projects, else every chunk. One range read of it replaces
 * hyparquet's one-read-per-column plan for a projected read (14 GETs per
 * store group), which the Worker's 6 simultaneous connections queue up. */
export function chunkSpan(rg: { columns: { meta_data?: { path_in_schema: string[]; data_page_offset: bigint | number; dictionary_page_offset?: bigint | number; total_compressed_size: bigint | number } }[] }, columns?: string[]): [number, number] {
  let start = Infinity
  let end = 0
  for (const c of rg.columns) {
    const m = c.meta_data!
    if (columns && !columns.includes(m.path_in_schema[0])) continue
    const data = Number(m.data_page_offset)
    const dict = m.dictionary_page_offset == null ? data : Number(m.dictionary_page_offset)
    const s = dict > 0 ? Math.min(dict, data) : data
    start = Math.min(start, s)
    end = Math.max(end, s + Number(m.total_compressed_size))
  }
  if (!(end > start)) throw new Error('row group has no column chunks to read')
  return [start, end]
}

/** Range reads merge spans closer than this (a gap is read and dropped):
 * a read costs a round trip (~60 ms from a Worker, and at most 6 in
 * flight) where 512 KiB more of it costs ~10 ms. */
export const RUN_GAP = 512 << 10
/** A merged range read's ceiling. */
export const RUN_BYTES = 2 << 20

/** Range reads in flight per merged read: a Worker holds at most 6
 * simultaneous connections, so more only queues (and holds buffers). */
export const RUN_READS = 6

/** Byte spans merged into range reads: sorted by start, a run grows while
 * the next span starts within `gap` bytes of its end and the run stays
 * within `max` bytes (a span bigger than `max` is a run of its own). */
export function planRuns<T extends { start: number; end: number }>(spans: T[], gap = RUN_GAP, max = RUN_BYTES): { start: number; end: number; items: T[] }[] {
  const runs: { start: number; end: number; items: T[] }[] = []
  for (const sp of [...spans].sort((a, b) => a.start - b.start)) {
    const last = runs[runs.length - 1]
    if (last && sp.start - last.end <= gap && Math.max(last.end, sp.end) - last.start <= max) {
      last.end = Math.max(last.end, sp.end)
      last.items.push(sp)
    } else runs.push({ start: sp.start, end: sp.end, items: [sp] })
  }
  return runs
}

/** A `FileSlice` over a buffer holding the file's bytes from `start`
 * (offsets stay absolute, as the revived row groups' are). */
export const bufferSlice = (buf: ArrayBuffer, start: number): FileSlice => ({
  byteLength: start + buf.byteLength,
  slice: async (s, e) => {
    const end = e ?? start + buf.byteLength
    if (s < start || end > start + buf.byteLength) throw new Error(`read [${s}, ${end}) outside the fetched [${start}, ${start + buf.byteLength})`)
    return buf.slice(s - start, end - start)
  },
})

/** One range read of a handle's file (traced as `fetch`). */
async function fetchRange(h: IndexHandle, start: number, end: number): Promise<ArrayBuffer> {
  const t0 = now()
  try {
    return await h.file.slice(start, end)
  } finally {
    h.trace?.('fetch', now() - t0)
  }
}

/** Read one row group (given its stored metadata JSON) via a subset
 * FileMetaData, as raw column records (pre-`toRow`): one range read of the
 * projected chunks (`chunkSpan`), or from `file` when the caller already
 * holds the bytes. The age index (variant `age`) carries its own columns
 * (`day,b,o`), not the shared `Row` shape. */
async function readGroupRaw(h: IndexHandle, rgJson: string, columns?: string[], held?: FileSlice): Promise<Record<string, unknown>[]> {
  const rg = reviveRowGroup(rgJson, h.schema)
  const metadata = { version: h.version, schema: h.schema, num_rows: rg.num_rows, row_groups: [rg], metadata_length: 0 } as unknown as Awaited<ReturnType<typeof parquetMetadataAsync>>
  let file = held
  if (!file) {
    const [s, e] = chunkSpan(rg as unknown as Parameters<typeof chunkSpan>[0], columns)
    file = bufferSlice(await fetchRange(h, s, e), s)
  }
  const t0 = now()
  const rows = (await parquetReadObjects({ file, metadata, columns, compressors })) as Record<string, unknown>[]
  h.trace?.('group', now() - t0, h.variant)
  return rows
}

/** Read one row group as shaped `Row`s (the handle's projection unless the
 * caller narrows it further). */
async function readGroup(h: IndexHandle, rgJson: string, columns?: string[], held?: FileSlice): Promise<Row[]> {
  return (await readGroupRaw(h, rgJson, columns ?? h.columns ?? undefined, held)).map(toRow(h))
}

export interface Span extends GroupSpan { rg: number }

/** Decoded row groups, per isolate (LRU by an estimated byte size).
 *
 * Decoding is the cold cost on the edge: ~100 ms of CPU per 8k-row group
 * (Workers Logs `cpuTime` 2026-09-15: a 25-group root subtree = 2.6 s CPU
 * against 0.3 s of I/O), and the same tier groups serve every view of a
 * scan — the root at each width, the bucket drills, both sides of every
 * diff. A generation dir is immutable, so `(date, variant, gen, rg)` is a
 * stable key. Rows are never mutated by readers (`classRow` copies). The cap
 * keeps well inside the isolate's 128 MB with concurrent requests. */
const GROUP_CACHE_CAP = 24 << 20
const ROW_BYTES = 160 // a shaped Row with a ~60-char path, roughly
/** Tier groups (`…|<rg>` → `Row[]`) and a cold footer's decoded groups
 * (`…|fg:<n>` → `BlobGroup[]`), one LRU, each entry with its estimated bytes. */
const groupCache = new Map<string, { v: unknown[]; bytes: number }>()
let groupCacheBytes = 0

export function cacheGet<T>(k: string): T[] | undefined {
  const hit = groupCache.get(k)
  if (!hit) return undefined
  groupCache.delete(k)
  groupCache.set(k, hit) // LRU: most recent last
  return hit.v as T[]
}

export function cachePut(k: string, v: unknown[], bytes: number): void {
  if (bytes > GROUP_CACHE_CAP) return
  const old = groupCache.get(k)
  if (old) {
    groupCache.delete(k)
    groupCacheBytes -= old.bytes
  }
  while (groupCacheBytes + bytes > GROUP_CACHE_CAP && groupCache.size) {
    const [k0, e0] = groupCache.entries().next().value as [string, { bytes: number }]
    groupCache.delete(k0)
    groupCacheBytes -= e0.bytes
  }
  groupCache.set(k, { v, bytes })
  groupCacheBytes += bytes
}

const groupKey = (h: IndexHandle, rg: number) => `${storeKey(h.env)}|${h.date}|${h.variant}|${h.gen}|${rg}`

/** Many row groups' shaped rows, each through the decoded-group cache; the
 * misses' projected chunks are fetched as merged range reads (`planRuns`:
 * neighbouring groups of a sort are adjacent in its file), `RUN_READS`
 * runs in flight, and each group decoded from its run's bytes. `f` maps each
 * group's rows (kept per group, in input order). */
async function readGroupsCached<T>(h: IndexHandle, groups: { rg: number; json: string }[], f: (rows: Row[]) => T): Promise<T[]> {
  const out: T[] = new Array(groups.length)
  const miss: { i: number; rg: number; json: string; start: number; end: number }[] = []
  groups.forEach((g, i) => {
    const hit = cacheGet<Row>(groupKey(h, g.rg))
    if (hit) {
      h.trace?.('gcache', 1)
      out[i] = f(hit)
      return
    }
    const [start, end] = chunkSpan(reviveRowGroup(g.json, h.schema) as unknown as Parameters<typeof chunkSpan>[0], h.columns ?? undefined)
    miss.push({ i, ...g, start, end })
  })
  await mapLimit(planRuns(miss), RUN_READS, async run => {
    const file = bufferSlice(await fetchRange(h, run.start, run.end), run.start)
    for (const g of run.items) {
      const rows = await readGroup(h, g.json, undefined, file)
      cachePut(groupKey(h, g.rg), rows, rows.length * ROW_BYTES)
      out[g.i] = f(rows)
    }
  })
  return out
}

/** Row groups in flight at once per read. Each group is its own range
 * fetch + decode; the fetch is ~200–240 ms of latency for ~280 KB, so the
 * reads are latency-bound, not bandwidth-bound: a root subtree's 24 groups
 * took three rounds at 8-wide (`Server-Timing` 2026-09-15: fetch 5.0 s
 * summed, 1.0 s wall) and a 7-day diff's 107 groups fourteen. 32 in flight
 * is ~9 MB of buffers at most — well inside the isolate's memory. */
export const GROUP_READS = 32

/** `Promise.all(items.map(f))` with at most `limit` in flight; results in
 * input order. */
export async function mapLimit<T, R>(items: T[], limit: number, f: (t: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length)
  let next = 0
  const worker = async () => {
    for (;;) {
      const i = next++
      if (i >= items.length) return
      out[i] = await f(items[i])
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return out
}

/** The span query's predicate, for a blob handle's in-memory groups — the
 * same test `selectSpans` sends D1, SQL NULL semantics included (a group with
 * no usr stats never matches a lens). */
export function groupMatches(g: { dMin: number; dMax: number; pMin: string; pMax: string; bMax: number; uMin?: string | null; uMax?: string | null }, rects: Rect[], bMin = 0, lens?: Lens, keyed = true): boolean {
  if (bMin > 0 && g.bMax < Math.floor(bMin)) return false
  const rect = (r: Rect) => g.dMax >= r.dLo && g.dMin <= r.dHi && (g.dMin !== g.dMax || (g.pMax >= r.pLo && g.pMin <= r.pHi))
  if (!lens) return rects.some(rect)
  if (g.uMin == null || g.uMax == null || !(g.uMin <= lens.key && g.uMax >= lens.key)) return false
  return keyed ? g.uMin !== g.uMax || rects.some(rect) : rects.some(rect)
}

/** Candidate row groups for a set of (depth, path-range) rectangles — one SQL
 * pass (no rg_json yet), carrying each group's stats so the caller can apply a
 * finer per-ask test. A group spanning a depth boundary resets path order, so
 * the path test only applies within a single depth (`d_min = d_max`). */
async function selectSpans(h: IndexHandle, rects: Rect[], cap = 4000, bMin = 0, lens?: Lens): Promise<Span[]> {
  if (h.mode !== 'd1') {
    const pass = (g: Parameters<typeof groupMatches>[0]) => groupMatches(g, rects, bMin, lens, lensSorted(h.variant))
    const out = h.mode === 'blob' ? h.groups.filter(pass) : await pqGroups(h, pass)
    if (out.length > cap) throw new Error(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
    return out
  }
  const where: string[] = []
  const binds: unknown[] = []
  // The (depth, path) rect; valid within a single primary-key group only
  // (single-depth for the path index, single-user for the lens index).
  const rectSql = '(d_max >= ? AND d_min <= ? AND (d_min <> d_max OR (p_max >= ? AND p_min <= ?)))'
  const keyed = lensSorted(h.variant)
  for (const r of rects) {
    if (lens && keyed) {
      // A `usr`-first sort: prune to groups whose usr range covers the lens
      // key; the rect is a secondary test that only holds inside a
      // single-key group.
      where.push(`(u_min <= ? AND u_max >= ? AND (u_min <> u_max OR ${rectSql}))`)
      binds.push(lens.key, lens.key, r.dLo, r.dHi, r.pLo, r.pHi)
    } else if (lens) {
      // A path-first sort (a store generation's `path`): the rect holds, and
      // the usr range is one more condition (groups mix users).
      where.push(`(${rectSql} AND u_min <= ? AND u_max >= ?)`)
      binds.push(r.dLo, r.dHi, r.pLo, r.pHi, lens.key, lens.key)
    } else {
      where.push(rectSql)
      binds.push(r.dLo, r.dHi, r.pLo, r.pHi)
    }
  }
  // Prune groups whose biggest row can't clear the shallowest threshold — the
  // footer path prunes by b_max during its scan, so without this a large
  // subtree returns far more candidate groups than it can draw and hits `cap`.
  const bFloor = bMin > 0 ? ' AND b_max >= ?' : ''
  const sql = `SELECT rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? AND (${where.join(' OR ')})${bFloor} ORDER BY rg LIMIT ${cap + 1}`
  if (bMin > 0) binds.push(Math.floor(bMin))
  const res = await h.env.DB!.prepare(sql).bind(h.date, d1Variant(h.env, h.variant), h.gen, ...binds).all<{ rg: number; d_min: number; d_max: number; p_min: string; p_max: string; b_max: number; row_start: number; row_end: number }>()
  if (res.results.length > cap) throw new Error(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
  return res.results.map(r => ({ rg: r.rg, dMin: num(r.d_min), dMax: num(r.d_max), pMin: r.p_min, pMax: r.p_max, bMax: num(r.b_max), rowStart: num(r.row_start), rowEnd: num(r.row_end) }))
}

/** Fetch stored metadata JSON for a set of row groups. */
async function fetchGroupJson(h: IndexHandle, rgs: number[]): Promise<Map<number, string>> {
  const out = new Map<number, string>()
  if (h.mode === 'blob') {
    const want = new Set(rgs)
    for (const g of h.groups) if (want.has(g.rg)) out.set(g.rg, g.rgJson)
    return out
  }
  if (h.mode === 'pq') {
    // A tier group's footer row is row `rg` of the footer parquet: read the
    // footer groups holding the asked rgs (the span query just decoded them,
    // so these are cache hits).
    const want = new Set(rgs)
    const fgs = h.footer.groups.filter(fg => rgs.some(rg => rg >= fg.rgStart && rg < fg.rgEnd))
    for (const g of (await mapLimit(fgs, GROUP_READS, fg => readFooterGroup(h, fg))).flat()) if (want.has(g.rg)) out.set(g.rg, g.rgJson)
    return out
  }
  for (let i = 0; i < rgs.length; i += 80) {
    const chunk = rgs.slice(i, i + 80)
    const sql = `SELECT rg, rg_json FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? AND rg IN (${chunk.map(() => '?').join(',')})`
    const res = await h.env.DB!.prepare(sql).bind(h.date, d1Variant(h.env, h.variant), h.gen, ...chunk).all<{ rg: number; rg_json: string }>()
    for (const r of res.results) out.set(r.rg, r.rg_json)
  }
  return out
}

// --- public read API ---------------------------------------------------------

/** Rows in the (depth, path) rectangle; `thrAt(depth)` prunes groups whose
 * biggest row can't clear the threshold. Same contract in both handle modes. */
export async function readRows(
  h: IndexHandle,
  dLo: number,
  dHi: number,
  pLo: string,
  pHi: string,
  thrAt?: (depth: number) => number,
  lens?: Lens,
): Promise<Row[]> {
  return readRects(h, [{ dLo, dHi, pLo, pHi }], thrAt, lens)
}

export interface Rect { dLo: number; dHi: number; pLo: string; pHi: string }

/** Rows in any of several (depth, path-range) rectangles — one read: the
 * candidate groups of all rects come from a few span queries (rects batched
 * per statement), each group is decoded once, and a row passes if some rect
 * holds it. What a lens's scattered claimed regions need. */
export async function readRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt?: (depth: number) => number,
  lens?: Lens,
  plan?: Span[],
): Promise<Row[]> {
  if (!rects.length) return []
  const kept = plan ?? await planRects(h, rects, thrAt, lens)
  // A row passes the lens iff its usr equals the key.
  const lensOk = (r: Row) => !lens || r.usr === lens.key
  const inRect = (r: Row) => rects.some(q => r.depth >= q.dLo && r.depth <= q.dHi && r.path >= q.pLo && r.path <= q.pHi)
  return decodeSpans(h, kept, r => inRect(r) && lensOk(r))
}

/** The row groups a `readRects` decodes — the span queries' candidates
 * (rects batched per statement) minus the groups whose biggest row can't
 * clear the threshold at their shallowest depth in the read. What
 * `disk-tree tiers plan` mirrors for the `path` sort. */
export async function planRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt?: (depth: number) => number,
  lens?: Lens,
): Promise<Span[]> {
  const dMin = Math.min(...rects.map(q => q.dLo))
  const RECTS_PER_QUERY = 20 // D1 binds: 4 per rect (+2 with a lens)
  const byRg = new Map<number, Span>()
  const t0 = now()
  for (let i = 0; i < rects.length; i += RECTS_PER_QUERY) {
    const spans = await selectSpans(h, rects.slice(i, i + RECTS_PER_QUERY), 4000, thrAt ? thrAt(dMin) : 0, lens)
    for (const s of spans) byRg.set(s.rg, s)
  }
  h.trace?.('spans', now() - t0)
  return [...byRg.values()].sort((a, b) => a.rg - b.rg).filter(s => !thrAt || s.bMax >= thrAt(Math.max(s.dMin, dMin)))
}

/** Decode a planned set of groups (cached per isolate, `GROUP_READS` in
 * flight) and keep the rows `keep` accepts — the tail shared by both sorts'
 * subtree reads. Caps the group count and the rows decoded: a broad lens (a
 * big user spread across the estate) can select few-enough groups but still
 * decode millions of rows and blow the Worker CPU. Error cleanly instead. */
async function decodeSpans(h: IndexHandle, kept: Span[], keep: (r: Row) => boolean): Promise<Row[]> {
  if (kept.length > 250) throw new Error('query too wide: drill deeper or raise minArea')
  const totalRows = kept.reduce((n, s) => n + (s.rowEnd - s.rowStart), 0)
  if (totalRows > 700_000) throw new Error('query too wide: drill deeper or raise minArea')
  let t0 = now()
  const jsons = await fetchGroupJson(h, kept.map(s => s.rg))
  h.trace?.('rgjson', now() - t0)
  h.trace?.('ngroups', kept.length)
  t0 = now()
  const perGroup = await readGroupsCached(h, kept.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep))
  h.trace?.('groups', now() - t0, h.variant)
  return perGroup.flat()
}

/** At most `n` of the calls it wraps running at once (the rest queue). */
export function limiter(n: number): <T>(f: () => Promise<T>) => Promise<T> {
  let active = 0
  const queue: (() => void)[] = []
  return async f => {
    if (active >= n) await new Promise<void>(r => queue.push(r))
    else active++
    try {
      return await f()
    } finally {
      const next = queue.shift()
      if (next) next()
      else active--
    }
  }
}

/** Merged range reads fetched `RUN_READS` at a time but handed to `each`
 * in order — a budgeted read stops on a prefix, so what it skips is the
 * tail (the lightest names, in impact order). `stop` is asked before each
 * fetch and each run: once it says so, the rest are skipped. */
export async function runsInOrder<R>(runs: R[], fetch: (r: R) => Promise<ArrayBuffer>, each: (r: R, buf: ArrayBuffer) => Promise<void>, stop: () => boolean): Promise<void> {
  const limit = limiter(RUN_READS)
  const bufs = runs.map(r => limit(() => (stop() ? Promise.resolve(null) : fetch(r))))
  for (const b of bufs) b.catch(() => {}) // a skipped run's failure is moot; an awaited one still throws
  for (let i = 0; i < runs.length; i++) {
    if (stop()) return
    const buf = await bufs[i]
    if (!buf || stop()) return
    await each(runs[i], buf)
  }
}

/** One row group's columns as arrays (no row objects), from bytes held. */
export async function decodeColumns(schema: SchemaElement[], rg: RowGroup, file: FileSlice, columns: string[]): Promise<Map<string, unknown[]>> {
  const n = Number(rg.num_rows)
  const out = new Map<string, unknown[]>(columns.map(c => [c, new Array(n)]))
  const metadata = { version: 1, schema, num_rows: rg.num_rows, row_groups: [rg], metadata_length: 0 } as unknown as FileMetaData
  await parquetRead({
    file, metadata, columns, compressors,
    onChunk: ({ columnName, columnData, rowStart }) => {
      const arr = out.get(columnName)!
      for (let i = 0; i < columnData.length; i++) arr[rowStart + i] = columnData[i]
    },
  })
  return out
}

/** The rows of `path`-sort groups named by ordinal whose path `keep`
 * accepts — the search index's read (`search.ts`: a name's `rgs` are
 * `path`-sort groups). A group in the decoded-group cache is filtered there;
 * the misses are fetched with their neighbours as merged range reads
 * (`planRuns`, `runsInOrder`), each group's `path` column decoded first, and
 * the other columns decoded — and rows built — only for a group with a kept
 * path, only for those rows (few of a group's rows match; the `path` column
 * is about half its decode and building 8k row objects most of the rest).
 * `stop(kept)` is asked before each group with the rows kept so far: once it
 * says so the remaining groups are skipped, and `read` names the groups
 * whose rows are all in `rows`. */
export async function readGroupsAt(h: IndexHandle, rgs: number[], keep: (path: string) => boolean, stop: (kept: number) => boolean = () => false): Promise<{ rows: Row[]; read: Set<number> }> {
  const read = new Set<number>()
  if (!rgs.length) return { rows: [], read }
  let t0 = now()
  const jsons = await fetchGroupJson(h, rgs)
  h.trace?.('rgjson', now() - t0)
  h.trace?.('ngroups', rgs.length)
  const missing = rgs.filter(rg => !jsons.has(rg))
  if (missing.length) throw new Error(`${h.variant} ${h.date}: no row group ${missing.join(', ')} in generation ${h.gen}`)
  t0 = now()
  const out: Row[] = []
  const miss: { rg: number; group: RowGroup; start: number; end: number }[] = []
  for (const rg of rgs) {
    const hit = cacheGet<Row>(groupKey(h, rg))
    if (hit) {
      h.trace?.('gcache', 1)
      for (const r of hit) if (keep(r.path)) out.push(r)
      read.add(rg)
      continue
    }
    const group = reviveRowGroup(jsons.get(rg)!, h.schema) as unknown as RowGroup
    const [start, end] = chunkSpan(group as unknown as Parameters<typeof chunkSpan>[0], h.columns ?? undefined)
    miss.push({ rg, group, start, end })
  }
  const rest = (h.columns ?? h.schema.slice(1).map(l => l.name)).filter(c => c !== 'path')
  const shape = toRow(h)
  let halted = false
  const halt = () => (halted ||= stop(out.length))
  await runsInOrder(planRuns(miss), run => fetchRange(h, run.start, run.end), async (run, buf) => {
    const file = bufferSlice(buf, run.start)
    for (const g of run.items) {
      if (halt()) return
      const t1 = now()
      const paths = (await decodeColumns(h.schema, g.group, file, ['path'])).get('path')!
      const hits: number[] = []
      for (let i = 0; i < paths.length; i++) if (keep(str(paths[i]))) hits.push(i)
      if (hits.length) {
        const cols = await decodeColumns(h.schema, g.group, file, rest)
        for (const i of hits) {
          const rec: Record<string, unknown> = { path: paths[i] }
          for (const [c, arr] of cols) rec[c] = arr[i]
          out.push(shape(rec))
        }
      }
      h.trace?.('group', now() - t1, h.variant)
      read.add(g.rg)
    }
  }, halt)
  h.trace?.('groups', now() - t0, h.variant)
  return { rows: out, read }
}

// --- the size-bucket sort (specs/path-store.md §1.3, §2.1) -------------------

/** The `bysize` span predicate, for a blob handle's in-memory groups — the
 * same test `selectSizeSpans` sends D1, and `disk-tree tiers plan`'s
 * `select_bysize`: `b_max ≥ ⌊thrMin⌋ ∧ p_max ≥ pLo ∧ p_min < pHi` for some
 * path range. Sound for any group (min/max stats bound every row it holds),
 * and tight because rows within a size bucket are path-sorted: a subtree is
 * one run per bucket. A lens needs the usr range to cover the key (NULL
 * stats never match, as in SQL). */
export function groupMatchesSize(g: { pMin: string; pMax: string; bMax: number; uMin?: string | null; uMax?: string | null }, ranges: { pLo: string; pHi: string }[], thrMin = 0, lens?: Lens): boolean {
  if (thrMin > 0 && g.bMax < Math.floor(thrMin)) return false
  if (lens && (g.uMin == null || g.uMax == null || !(g.uMin <= lens.key && g.uMax >= lens.key))) return false
  return ranges.some(r => g.pMax >= r.pLo && g.pMin < r.pHi)
}

/** Candidate row groups of the `bysize` sort for a set of path ranges at
 * one byte floor — one SQL pass per batch of ranges, no depth rect (the
 * depth test is per row, §1.3 "attenuation"). */
async function selectSizeSpans(h: IndexHandle, ranges: { pLo: string; pHi: string }[], thrMin: number, cap = 4000, lens?: Lens): Promise<Span[]> {
  if (h.mode !== 'd1') {
    const pass = (g: Parameters<typeof groupMatchesSize>[0]) => groupMatchesSize(g, ranges, thrMin, lens)
    const out = h.mode === 'blob' ? h.groups.filter(pass) : await pqGroups(h, pass)
    if (out.length > cap) throw new Error(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
    return out
  }
  const where = ranges.map(() => '(p_max >= ? AND p_min < ?)').join(' OR ')
  const binds: unknown[] = ranges.flatMap(r => [r.pLo, r.pHi])
  const floor = thrMin > 0 ? ' AND b_max >= ?' : ''
  if (thrMin > 0) binds.push(Math.floor(thrMin))
  const lensSql = lens ? ' AND u_min <= ? AND u_max >= ?' : ''
  if (lens) binds.push(lens.key, lens.key)
  const sql = `SELECT rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? AND (${where})${floor}${lensSql} ORDER BY rg LIMIT ${cap + 1}`
  const res = await h.env.DB!.prepare(sql).bind(h.date, d1Variant(h.env, h.variant), h.gen, ...binds).all<{ rg: number; d_min: number; d_max: number; p_min: string; p_max: string; b_max: number; row_start: number; row_end: number }>()
  if (res.results.length > cap) throw new Error(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
  return res.results.map(r => ({ rg: r.rg, dMin: num(r.d_min), dMax: num(r.d_max), pMin: r.p_min, pMax: r.p_max, bMax: num(r.b_max), rowStart: num(r.row_start), rowEnd: num(r.row_end) }))
}

/** The deepest row the tier holds (`MAX(d_max)` over its groups), memoized
 * per generation — the depth an unbounded read's floor is taken at when the
 * threshold falls with depth (`atten < 1`). */
const maxDepths = new Map<string, Promise<number>>()
async function tierMaxDepth(h: IndexHandle): Promise<number> {
  if (h.mode === 'blob') return h.groups.reduce((m, g) => Math.max(m, g.dMax), 0)
  if (h.mode === 'pq') {
    // The footer groups' `d_max` stats bound it; decode only where a group has none.
    let m = 0
    for (const fg of h.footer.groups) {
      m = Math.max(m, fg.bounds ? fg.bounds.dMax : (await readFooterGroup(h, fg)).reduce((x, g) => Math.max(x, g.dMax), 0))
    }
    return m
  }
  const k = `${storeKey(h.env)}|${h.date}|${h.variant}|${h.gen}`
  return shared(maxDepths, k, async () => {
    const r = await h.env.DB!.prepare('SELECT MAX(d_max) AS d FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ?').bind(h.date, d1Variant(h.env, h.variant), h.gen).first<{ d: number | null }>()
    return num(r?.d)
  }, 10_000)
}

/** The lowest per-depth threshold a read over `rects` applies: `thrAt` is
 * monotone in depth (up for `atten ≥ 1`, down for `atten < 1`), so it is the
 * smaller of its values at each rect's shallowest and deepest depth — the
 * deepest being the tier's own when a rect is unbounded. */
async function sizeFloor(h: IndexHandle, rects: Rect[], thrAt: (depth: number) => number): Promise<number> {
  let floor = Infinity
  for (const q of rects) {
    const dHi = q.dHi < 1e9 ? q.dHi : thrAt(q.dLo + 1) < thrAt(q.dLo) ? await tierMaxDepth(h) : q.dLo
    floor = Math.min(floor, thrAt(q.dLo), thrAt(Math.max(dHi, q.dLo)))
  }
  return floor
}

/** The row groups a `readSizeRects` decodes: every group of the size sort
 * whose path range meets a rect's and whose top bucket clears the read's
 * lowest threshold — the planner's `select_bysize`, exactly. */
export async function planSizeRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt: (depth: number) => number,
  lens?: Lens,
): Promise<Span[]> {
  const thrMin = await sizeFloor(h, rects, thrAt)
  const RANGES_PER_QUERY = 40 // D1 binds: 2 per range (+3)
  const byRg = new Map<number, Span>()
  const t0 = now()
  for (let i = 0; i < rects.length; i += RANGES_PER_QUERY) {
    for (const s of await selectSizeSpans(h, rects.slice(i, i + RANGES_PER_QUERY), thrMin, 4000, lens)) byRg.set(s.rg, s)
  }
  h.trace?.('spans', now() - t0)
  return [...byRg.values()].sort((a, b) => a.rg - b.rg)
}

/** A thresholded subtree read from the `bysize` sort (§2.1): the same
 * rows `readRects` returns from `path` at the same rects and `thrAt`, but
 * decoded from the groups above the threshold instead of the groups under
 * the path — `(rows under P with size ≥ thr) / group + #buckets` groups, so a
 * flat directory's children or a fleet root's view cost what they draw. The
 * per-row test is exact: depth in a rect, path in its range, `size ≥
 * thrAt(depth)` (a store whose rows are owner slices thresholds per slice). */
export async function readSizeRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt: (depth: number) => number,
  lens?: Lens,
  plan?: Span[],
): Promise<Row[]> {
  if (!rects.length) return []
  const kept = plan ?? await planSizeRects(h, rects, thrAt, lens)
  const lensOk = (r: Row) => !lens || r.usr === lens.key
  const inRect = (r: Row) => rects.some(q => r.depth >= q.dLo && r.depth <= q.dHi && r.path >= q.pLo && r.path < q.pHi)
  return decodeSpans(h, kept, r => r.size >= thrAt(r.depth) && inRect(r) && lensOk(r))
}

/** A point lookup `(depth, path)` or a one-level range under a prefix. */
export type Ask = { depth: number; path: string } | { depth: number; under: string }

const askRect = (a: Ask): Rect =>
  'path' in a
    ? { dLo: a.depth, dHi: a.depth, pLo: a.path, pHi: a.path }
    : { dLo: a.depth, dHi: a.depth, pLo: a.under + '/', pHi: a.under + '0' } // '0' sorts just past '/'

const groupMayHold = (g: Span, a: Ask): boolean => {
  if (g.dMax < a.depth || g.dMin > a.depth) return false
  if (g.dMin !== g.dMax) return true
  return 'path' in a ? !(g.pMax < a.path || g.pMin > a.path) : !(g.pMax < a.under + '/' || g.pMin > a.under + '0')
}

/** Point lookups: exact `(depth, path)` rows or `(depth, under-prefix)`
 * ranges, many at once (the totals manifest's ~8k prefixes) — `keep` is the
 * caller's exact test over the rows of the candidate groups. */
export async function readAsks(
  h: IndexHandle,
  asks: Ask[],
  keep: (r: Row) => boolean,
  { columns, maxGroups = 60 }: { columns?: string[]; maxGroups?: number } = {},
): Promise<{ rows: Row[]; groups: number }> {
  // Collapse asks to one rectangle per depth (min..max path) to keep the SQL
  // small; the exact ask set is enforced by `keep` after the read.
  const byDepth = new Map<number, { pLo: string; pHi: string }>()
  for (const a of asks) {
    const r = askRect(a)
    const cur = byDepth.get(a.depth)
    byDepth.set(a.depth, cur ? { pLo: cur.pLo < r.pLo ? cur.pLo : r.pLo, pHi: cur.pHi > r.pHi ? cur.pHi : r.pHi } : { pLo: r.pLo, pHi: r.pHi })
  }
  const rects = [...byDepth.entries()].map(([d, r]) => ({ dLo: d, dHi: d, pLo: r.pLo, pHi: r.pHi }))
  // A per-depth [min,max] rectangle over-selects the groups between the
  // lowest and highest ask; narrow to groups an actual ask falls in.
  let t0 = now()
  const cand = await selectSpans(h, rects)
  h.trace?.('spans', now() - t0)
  const spans = cand.filter(s => asks.some(a => groupMayHold(s, a)))
  if (spans.length > maxGroups) throw new Error(`lookup too wide: ${spans.length} row groups (cap ${maxGroups})`)
  t0 = now()
  const jsons = await fetchGroupJson(h, spans.map(s => s.rg))
  h.trace?.('rgjson', now() - t0)
  h.trace?.('ngroups', spans.length)
  t0 = now()
  // a column subset (the totals manifest) bypasses the cache: cached rows are whole
  const perGroup = columns
    ? await mapLimit(spans, GROUP_READS, async s => {
      const j = jsons.get(s.rg)
      return j ? (await readGroup(h, j, columns)).filter(keep) : []
    })
    : await readGroupsCached(h, spans.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep))
  h.trace?.('groups', now() - t0)
  return { rows: perGroup.flat(), groups: spans.length }
}

/** Raw rows (pre-`toRow` records) for a single `(depth, path)` point lookup —
 * the age index (variant `age`), whose columns (`day,b,o`) aren't the shared
 * `Row` shape but which sorts `(depth, path, day)` so a path's day rows are one
 * contiguous run. Same span-select + row-group-prune path as the readers above. */
export async function readPoint(h: IndexHandle, depth: number, path: string, columns?: string[]): Promise<Record<string, unknown>[]> {
  const ask: Ask = { depth, path }
  const cand = await selectSpans(h, [askRect(ask)])
  const spans = cand.filter(s => groupMayHold(s, ask))
  if (spans.length > 60) throw new Error(`age lookup too wide: ${spans.length} row groups`)
  const jsons = await fetchGroupJson(h, spans.map(s => s.rg))
  const perGroup = await mapLimit(spans, GROUP_READS, async s => {
    const j = jsons.get(s.rg)
    if (!j) return []
    return (await readGroupRaw(h, j, columns)).filter(r => str(r.path) === path)
  })
  return perGroup.flat()
}
