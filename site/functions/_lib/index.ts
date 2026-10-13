/**
 * A scan's index tiers (`<dir>/path-index[-coarse<E>][-by-user].parquet`) as
 * a row-group-pruned range reader — shared by `/api/subtree` (pixel-budget
 * drill), `/api/diff`, `/api/series` and the owner totals (exact bytes per
 * live assignment, `_lib/ownerTotals.ts`).
 *
 * Each file is sorted (depth, path) (or (usr, depth, path)): the descendants
 * of P at each depth are one contiguous run, so any prefix query is a few
 * row-group selections on the footer stats plus ranged reads of just those
 * groups.
 *
 * The footer stats live in D1 (specs/done/path-agnostic-serving.md §2.1):
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
import type { Store } from '@rdub/file-tree'
import { R2Store } from '@rdub/file-tree/stores/r2'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type FileMetaData, parquetMetadata, parquetMetadataAsync, parquetRead, parquetReadObjects, type RowGroup } from 'hyparquet'
import type { Env } from './auth.js'
import { newestManifest } from './manifests.js'
import { inFlight, join, JOIN, type Join, reqOf, shared, STALLED, track } from './shared.js'
import { d1Variant, isPrimary, PRIMARY_STORE, storeKey } from './stores.js'
import { compressors } from './zstd.js'

/** A leaf of the stored parquet schema (`index_schema.schema_json`). */
export interface SchemaElement { type: string; name: string; repetition_type: string; converted_type?: string }


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
  /** An interval-store row (one per path): its owner slices' bytes by user (`usr` is null there). */
  us?: Record<string, number> | null
  /** An interval-store row's validity `[vf, vt)` (epoch seconds of scans). */
  vf?: number
  vt?: number
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
  /** An interval-store sort read as of a scan (`openInterval`): its epoch. Every group and row is
   *  tested `vf ≤ asOf < vt` (specs/interval-store.md §2.6). */
  asOf?: number
  /** Where the handle's own files are read from, when not the deployment's store (the interval
   *  store on `INDEX_R2`). */
  src?: ByteStore
  /** An interval store's per-scan runs read beside this handle (specs/interval-store.md §2.7; oldest first): each a
   *  handle on the run's own sort. A read plans every tier, and combines the rows of one version across tiers (the
   *  smallest `vt`: a run's close record ends a version an older tier still holds open) before testing liveness
   *  (`combineLive`). Every usable run, also at a scan before its first: liveness there doesn't need it, but a row's
   *  `vt` does (a diff's lookups trust it to say whether the version is live at the other scan, `Ask`) — except for a
   *  request that names every scan it compares (`asOfScans`), which leaves out the runs begun after them. */
  runs?: IndexHandle[]
  /** An interval store's data file (its R2 key, revision-suffixed): its decoded row groups are held in the colo
   *  under it (`coloGroups`). */
  dataKey?: string
  /** A run's handle (its key, `deltas/<first>[_<last>]`): a group can matter at `asOf` when a version in it opened by
   *  then (`vfMin ≤ asOf`), live or not — a close record is not live, yet it ends one. */
  run?: string
}

/** Ranged reads of whole-key objects (`S3Store`'s `get` shape). `tail`, where the store has suffix
 *  reads (R2): an object's last `n` bytes and its size in one request, with no size probe first. */
export interface ByteStore {
  get(key: string, range: { offset: number; length: number }): Promise<{ bytes: Uint8Array; totalSize?: number }>
  tail?(key: string, n: number): Promise<{ bytes: Uint8Array; totalSize?: number }>
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
  /** An interval-store group's `vf` and `vt` bounds. */
  vfMin?: number
  vfMax?: number
  vtMin?: number
  vtMax?: number
  /** An interval-store group's smallest size key (`b_min`) and its segment (`seg`; groups never mix segments):
   *  with its neighbours', what bounds a size sort's group tighter than its min/max (`sizeNeighbours`). */
  bMin?: number
  seg?: number
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
  bounds: { dMin: number; dMax: number; pMin: string; pMax: string; bMax: number; uMin: string | null; uMax: string | null; vfMin?: number; vtMax?: number } | null
}
interface FooterIndex {
  key: string
  metadata: FileMetaData
  groups: FooterGroup[]
  /** A compact footer (`openIvFooter`): `metadata` holds only `key_value_metadata` and the groups no `meta`;
   *  this parses the whole footer, for decoding a footer group the colo doesn't hold decoded. */
  full?: () => Promise<FooterIndex>
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
  return !!(accessKeyId && secretAccessKey && storeTarget(env).bucket)
}

/** Where the index store lives: `STORE_ENDPOINT`/`STORE_BUCKET`/`STORE_REGION`
 *  point any S3-compatible store (R2: the account's S3 endpoint + `auto`);
 *  endpoint and region unset = GCS. The bucket has no default: unset, the
 *  store isn't ready (`storeReady`). One place, so every proxy resolves the
 *  same store. */
export const storeTarget = (env: Env) => ({
  endpoint: env.STORE_ENDPOINT ?? 'https://storage.googleapis.com',
  bucket: env.STORE_BUCKET ?? '',
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

/** Where the small served files are read: the snapshot JSONs (`snapshots/…`) and the extras sidecars
 * (`<gen dir>/attr.tsv{,.idx.json}`), under their store keys. `SERVED_R2=1`: from `INDEX_R2` (the job
 * copies them there, `dt-cloud publish-r2 -S`), so a page's header, scan list and provenance never leave
 * Cloudflare; unset: through the store seam, as before. A deployment whose `STORE_*` is already R2 reads
 * the same bytes either way; the seam's other branch goes once every deployment publishes and binds. */
export function servedStore(env: Env, prefixes: string[]): Store {
  if (!env.SERVED_R2) return S3Store({ ...storeTarget(env), prefixes, ...storeCreds(env) })
  if (!env.INDEX_R2) throw new Error('SERVED_R2 is set but the INDEX_R2 binding is missing')
  return R2Store(env.INDEX_R2 as unknown as Parameters<typeof R2Store>[0], { prefixes })
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
    // Both sorts' generations: a view may read either, and `bysize` is repointed on its own (a re-cut,
    // `index-sync -v bysize`), so a key over `path` alone kept serving the old cut's cached views.
    const { results } = isPrimary(env)
      ? await env.DB.prepare(`SELECT date, variant, gen FROM index_schema WHERE variant IN ('path', 'bysize') AND date IN (${marks}) ORDER BY date, variant`).bind(...chunk).all<{ date: string; variant: string; gen: string | null }>()
      : await env.DB.prepare(`SELECT date, variant, gen FROM index_schema WHERE store = ? AND variant IN (?, ?) AND date IN (${marks}) ORDER BY date, variant`).bind(storeKey(env), d1Variant(env, 'path'), d1Variant(env, 'bysize'), ...chunk).all<{ date: string; variant: string; gen: string | null }>()
    for (const r of results) gens.set(r.date, `${gens.get(r.date) ?? ''}${r.variant}:${r.gen ?? ''},`)
  }
  let h = 0x811c9dc5
  // The interval store's dates: their answers are the store's generation's, keyed apart.
  if (intervalsOn(env)) {
    const held = await ivScans(env)
    for (const d of uniq) if (held.has(d)) gens.set(d, `iv:${env.INTERVAL_STORE_GEN}${ivRev(env)}`)
  }
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

/** `pathScans` with each scan's index generation stamp. */
export function pathScanGens(env: Env): Promise<{ results: { date: string; gen: string | null }[] }> {
  return isPrimary(env)
    ? env.DB!.prepare(`SELECT date, gen FROM index_schema WHERE variant = 'path'`).all<{ date: string; gen: string | null }>()
    : env.DB!.prepare(`SELECT date, gen FROM index_schema WHERE store = ? AND variant = ?`).bind(storeKey(env), d1Variant(env, 'path')).all<{ date: string; gen: string | null }>()
}

/** A generation stamp's instant, when it is one: `YYYYMMDDHHMM` (UTC, the m3
 * ingest's capture stamp) → `YYYY-MM-DDTHH:MMZ`; anything else → null. */
export function genInstant(gen: string | null): string | null {
  const m = gen ? /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(gen) : null
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}Z` : null
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
  if (intervalsOn(env)) {
    const h = await openInterval(env, date, variant)
    if (h) return h
  }
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

// --- the interval store (specs/interval-store.md): every scan of a generation in one set of sorts ---

/** `PATH_STORE=intervals` with `INTERVAL_STORE_GEN` and the `INDEX_R2` binding: a date the generation
 *  holds reads its `path` / `bysize` sorts from the interval store, as of that scan; any other date or
 *  variant (a lens's user-first sort, the age index, a date past the generation) reads as before. */
export const intervalsOn = (env: Env): boolean => env.PATH_STORE === 'intervals' && !!env.INTERVAL_STORE_GEN && !!env.INDEX_R2

/** A request's path store: `PATH_STORE=opt-in` reads the interval store only for requests asking
 *  `ps=iv` (so one deployment serves both, side by side); a secondary store (`store=`) never does. */
export function withPathStore<C extends { request: Request; env: Env }>(ctx: C): C {
  const { env } = ctx
  if (env.PATH_STORE !== 'opt-in' && env.PATH_STORE !== 'intervals') return ctx
  const u = new URL(ctx.request.url).searchParams
  const on = !u.get('store') && (env.PATH_STORE === 'intervals' || u.get('ps') === 'iv')
  const w = (ctx as { waitUntil?: unknown }).waitUntil
  return { ...ctx, env: { ...env, PATH_STORE: on ? 'intervals' : undefined }, ...(typeof w === 'function' ? { waitUntil: w.bind(ctx) } : {}) }
}
/** Which path store answers `env`'s reads of a date the interval store holds: '' (per-scan), else the
 *  generation (folded or sliced). A per-isolate memo of what a date has keys by it, as by `storeKey`:
 *  under `PATH_STORE=opt-in` one isolate serves both stores. */
export const pathStoreKey = (env: Env): string => (intervalsOn(env) ? `iv:${env.INTERVAL_STORE_GEN}${ivRev(env)}${isSliced(env) ? ':s' : ''}` : '')
/** `env` reading per-scan stores only: what a read needs whose rows must be owner slices (a user lens,
 *  an owner pool, owner totals) — the interval store folds a path's slices into one row. */
export const perScan = (env: Env): Env => (intervalsOn(env) ? { ...env, PATH_STORE: undefined } : env)
/** `env` reading owner-slice rows (a user lens, an owner pool, owner totals, class scopes): the interval
 *  store's slice sorts where its generation has them (`openInterval`), else per-scan stores. */
export const slices = (env: Env): Env => (intervalsOn(env) ? { ...env, [IV_SLICED]: true } as Env : env)
/** `env` reading the interval store's folded sorts (one row per path, exact totals) again. */
export const folded = (env: Env): Env => (isSliced(env) ? { ...env, [IV_SLICED]: false } as Env : env)
/** `env` for a request that reads the interval store as of `dates` alone (a view's scan, a diff's two): its
 *  handles leave out the runs that begin after the newest of them (`openInterval`). A run's rows there are
 *  versions opened later (live at none of the dates) and close records ending versions later (each still
 *  live at every date it was), so the rows a read keeps are the same; only their `vt` differs — a version a
 *  later run closes reads as open — and a read comparing rows across scans (a diff's bounded lookups)
 *  compares only scans it named, all at or before that horizon. */
export const asOfScans = (env: Env, dates: string[]): Env => (intervalsOn(env) ? { ...env, [IV_ASOF]: dates } as Env : env)
export const IV_ASOF = Symbol('interval-store request scans')
/** The marker `slices` sets on an env (a symbol: not a binding or var; object spreads carry it). */
export const IV_SLICED = Symbol('interval-store slices')
const isSliced = (env: Env): boolean => !!(env as Env & { [IV_SLICED]?: boolean })[IV_SLICED]
export const IV_PREFIX = 'interval-store'
/** `INTERVAL_STORE_REV`: a generation's files were rewritten in place (a re-cut): every cache keyed by
 *  them (colo ranges and footers, isolate handles and groups, response keys) takes the revision. */
const ivRev = (env: Env): string => (env.INTERVAL_STORE_REV ? `@${env.INTERVAL_STORE_REV}` : '')
const ivKey = (env: Env, key: string): string => (env.INTERVAL_STORE_REV ? `${key}?rev=${env.INTERVAL_STORE_REV}` : key)
/** The sorts the store serves (the reads sort is looked up by `ivLastRead`). */
const IV_SORTS = new Set(['path', 'bysize', 'reads'])
/** Under `slices(env)`: each per-scan variant a sliced read opens → the store's owner-slice sort (`bysize`'s
 *  keyed on each path's total, as the per-scan store's is: `keyedOnTotal`). */
const IV_SLICE_SORTS = new Map([['path', 'slices'], ['bysize', 'slices-bytotal'], ['bysize-user', 'slices-bysize-user']])
/** Generations found without slice sorts (their sliced reads go per-scan), held a minute. */
const ivNoSlices = new Map<string, number>()
/** The sort a read of each sort also opens (its footer is prefetched with it). */
const IV_SIBLING = new Map([['path', 'bysize'], ['bysize', 'path'], ['slices', 'slices-bytotal'], ['slices-bytotal', 'slices']])
/** A served sort's footer, opened once per isolate for every scan (a generation's files are immutable). */
const ivFooters = new Map<string, Promise<FooterIndex>>()
function ivFooter(env: Env, file: string): Promise<FooterIndex> {
  const key = ivKey(env, `${IV_PREFIX}/${env.INTERVAL_STORE_GEN!}/served/${file}.groups.parquet`)
  return shared(ivFooters, `${storeKey(env)}|${key}`, () => openIvFooter(env, key, r2Bytes(env.INDEX_R2!)), 30_000, ivJoin(env, 'footer'))
}

/** `INDEX_R2` as a `ByteStore`. */
export function r2Bytes(r2: R2Bucket): ByteStore {
  return {
    async get(key0, { offset, length }) {
      // A `?…` suffix versions a key for the caches (`ivKey`); the object is the key before it.
      const key = key0.split('?')[0]
      const o = await r2.get(key, { range: { offset, length } })
      if (!o) throw Object.assign(new Error(`${key}: not found`), { name: 'NotFoundError' })
      return { bytes: new Uint8Array(await o.arrayBuffer()), totalSize: o.size }
    },
    async tail(key0, n) {
      const key = key0.split('?')[0]
      const o = await r2.get(key, { range: { suffix: n } })
      if (!o) throw Object.assign(new Error(`${key}: not found`), { name: 'NotFoundError' })
      return { bytes: new Uint8Array(await o.arrayBuffer()), totalSize: o.size }
    },
  }
}

// --- the store's per-scan runs (specs/interval-store.md §2.7, `interval_append`) ---
//
// A base generation plus one small run per newer scan under `deltas/<first>[_<last>]/served/` (the base's sorts, cut
// from the scan's changes: versions opened there and close records), merged on a binary counter and listed by the
// newest immutable `manifests/<scan id>.json` (ids sort in time order). A run is read as one more tier of every sort
// (`IndexHandle.runs`). A broken run — its files missing or unreadable (a publish caught mid-copy, a corrupt object) —
// cuts the stack there for `IV_BROKEN_TTL`: its scans and every later run's leave the store (they read per-scan), the
// ones before it answer as before, never a failed request (`ivRetry`).

/** A run a manifest lists (`interval_append.manifest`). */
interface IvRun { key: string; first: string; last: string; level: number; scans: string[] }
interface IvManifest { gen: string; date: string; scans: string[]; stamps: Record<string, number>; runs: IvRun[] }
/** The store's scans (id → epoch), the base's alone, and the usable runs (oldest first). `cut`: a broken run cut the
 *  stack. */
interface IvState { scans: Map<string, number>; base: Set<string>; runs: IvRun[]; cut: boolean }
/** How long a state stands before the manifests are listed again (a new scan's run shows up within it). */
export const IV_STATE_TTL = 60_000
/** How long a broken run stays cut out (then it is tried again: a publish caught mid-copy heals). */
export const IV_BROKEN_TTL = 30_000
const ivStates = new Map<string, { at: number; p: Promise<IvState>; ok?: IvState }>()
/** The bounds on a read joining an interval-store promise another request started (its state, a handle, a footer
 *  or footer group, a row group) before it evicts it and reads for itself (`shared.ts`' `JOIN`). Workers freeze a
 *  cancelled request's promises: a viewer leaving a slow lens view left its handle or groups pending forever, and
 *  every later read of them in the isolate waited on them with no response (gcs, 2026-10-10); a fixed 10 s wait per
 *  memo then chained to ~60 s (prod, 2026-10-10). A knob for tests. */
export const IV_JOIN = JOIN
/** A memo `name` of the interval store, joined as `env`'s request (`Server-Timing`: `join-<name>`). */
const ivJoin = (env: Env, name: string): Join => ({ name, req: reqOf(env) })
const ivBroken = new Map<string, { at: number; error: string }>()
const ivGenKey = (env: Env): string => `${storeKey(env)}|${env.INTERVAL_STORE_GEN}${ivRev(env)}`

/** A read of an interval store's run failed: the run is cut out (`ivMarkBroken`) and the read can be retried
 *  (`ivRetry`), when the run's scans read per-scan. */
export class IvTierBroken extends Error {
  constructor(readonly run: string, cause: unknown) {
    super(`interval store run ${run} is broken: ${cause instanceof Error ? cause.message : String(cause)}`)
    this.name = 'IvTierBroken'
  }
}

/** Cut run `key` out of the env's store for `IV_BROKEN_TTL` (logged once per window), and drop the held state. */
export function ivMarkBroken(env: Env, key: string, error: unknown): IvTierBroken {
  const e = error instanceof IvTierBroken ? error : new IvTierBroken(key, error)
  const k = `${ivGenKey(env)}|${key}`
  const was = ivBroken.get(k)
  if (!was || Date.now() - was.at >= IV_BROKEN_TTL) {
    ivBroken.set(k, { at: Date.now(), error: e.message })
    console.error(`${e.message}; its scans and every later run's read per-scan for ${IV_BROKEN_TTL / 1000}s`)
  }
  ivStates.delete(ivGenKey(env))
  return e
}

/** `f`, once more when it failed on a broken run (`IvTierBroken`): the stack is cut by then, so the retry reads the
 *  run's scans per-scan and the rest as before. */
export async function ivRetry<T>(f: () => Promise<T>): Promise<T> {
  try {
    return await f()
  } catch (e) {
    if (!(e instanceof IvTierBroken)) throw e
    return f()
  }
}

/** The newest manifest (`manifests.ts`: a scan's `manifests/<scan id>.json`, or a merge's revision of it
 *  `<scan id>.mNNN.json`), or null (none, or a bucket without `list`). */
async function ivManifest(env: Env): Promise<IvManifest | null> {
  const r2 = env.INDEX_R2! as R2Bucket & { list?: R2Bucket['list'] }
  if (typeof r2.list !== 'function') return null
  const prefix = `${IV_PREFIX}/${env.INTERVAL_STORE_GEN}/manifests/`
  const names: string[] = []
  let cursor: string | undefined
  do {
    const page = await r2.list({ prefix, ...(cursor ? { cursor } : {}) })
    for (const o of page.objects) names.push(o.key.slice(prefix.length))
    cursor = page.truncated ? page.cursor : undefined
  } while (cursor)
  const name = newestManifest(names)
  if (name === null) return null
  const o = await env.INDEX_R2!.get(`${prefix}${name}`)
  if (!o) return null
  const m = await o.json<IvManifest>()
  if (!Array.isArray(m.runs) || !Array.isArray(m.scans) || typeof m.stamps !== 'object') throw new Error(`interval store: bad ${prefix}${name}`)
  return m
}

/** The generation's `scans.json` and newest manifest, as one document. */
interface IvStateDoc { scans: { id: string; ts: number }[]; manifest: IvManifest | null }

/** `IvStateDoc` through the colo cache for `IV_STATE_TTL`: a new isolate's first read starts from it instead of
 *  three R2 operations in series (`scans.json`, the manifests' `list`, the newest's `get`). A new scan's run
 *  shows up within the colo's TTL plus the isolate's. */
async function ivStateDoc(env: Env): Promise<IvStateDoc> {
  const gen = env.INTERVAL_STORE_GEN!
  const ck = coloKey(env, ivKey(env, `${IV_PREFIX}/${gen}/scans.json`), 'state=1')
  const hit = await colo().match(ck)
  if (hit) return hit.json<IvStateDoc>()
  const [o, manifest] = await Promise.all([env.INDEX_R2!.get(`${IV_PREFIX}/${gen}/scans.json`), ivManifest(env)])
  if (!o) throw new Error(`interval store ${gen}: no scans.json`)
  const doc: IvStateDoc = { scans: (await o.json<{ scans: { id: string; ts: number }[] }>()).scans, manifest }
  await colo().put(ck, new Response(JSON.stringify(doc), { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${IV_STATE_TTL / 1000}` } }))
  return doc
}

async function loadIvState(env: Env): Promise<IvState> {
  const doc = await ivStateDoc(env)
  const scans = new Map(doc.scans.map(x => [x.id, x.ts]))
  const base = new Set(scans.keys())
  const m = doc.manifest
  const runs: IvState['runs'] = []
  let cut = false
  for (const r of m?.runs ?? []) {
    const b = ivBroken.get(`${ivGenKey(env)}|${r.key}`)
    const ts = r.scans.map(x => m!.stamps[x])
    if ((b && Date.now() - b.at < IV_BROKEN_TTL) || ts.some(t => typeof t !== 'number')) { cut = true; break }
    runs.push(r)
    r.scans.forEach((x, i) => scans.set(x, ts[i]))
  }
  return { scans, base, runs, cut }
}

/** The store's state, re-read once it is `IV_STATE_TTL` old (`IV_BROKEN_TTL` when cut); a failed re-read keeps
 *  serving the last good one. */
async function ivState(env: Env): Promise<IvState> {
  const k = ivGenKey(env)
  const cur = ivStates.get(k)
  const age = cur ? Date.now() - cur.at : Infinity
  if (cur && age < (cur.ok?.cut ? IV_BROKEN_TTL : IV_STATE_TTL)) {
    if (cur.ok) return cur.ok
    // A load another request started: joined (`IV_JOIN`), else read afresh here.
    const got = await join(cur.p, ivJoin(env, 'state'))
    if (got !== STALLED) return got
    if (ivStates.get(k) === cur) ivStates.delete(k)
    const now = ivStates.get(k)
    if (now && now !== cur) return ivState(env)
  }
  const last = cur?.ok
  const entry: { at: number; p: Promise<IvState>; ok?: IvState } = { at: Date.now(), p: Promise.resolve(null as unknown as IvState) }
  entry.p = loadIvState(env).then(
    st => { entry.ok = st; return st },
    e => { if (ivStates.get(k) === entry) ivStates.delete(k); if (last) return last; throw e },
  )
  ivStates.set(k, entry)
  track(entry.p, ivJoin(env, 'state'), () => { if (ivStates.get(k) === entry) ivStates.delete(k) })
  return entry.p
}

/** The generation's scans, its runs' included (id → epoch). */
async function ivScans(env: Env): Promise<Map<string, number>> {
  return (await ivState(env)).scans
}

/** A run's served sort: its footer (memoized as the base's), and the data file there (R2 `head`, where the binding
 *  has it), so a run listed before its files are whole is broken here, not mid-read. */
function ivRunFooter(env: Env, run: string, file: string): Promise<FooterIndex> {
  const dir = `${IV_PREFIX}/${env.INTERVAL_STORE_GEN!}/${run}/served`
  const key = ivKey(env, `${dir}/${file}.groups.parquet`)
  return shared(ivFooters, `${storeKey(env)}|${key}`, async () => {
    const r2 = env.INDEX_R2! as R2Bucket & { head?: R2Bucket['head'] }
    const [footer, data] = await Promise.all([openIvFooter(env, key, r2Bytes(env.INDEX_R2!)), typeof r2.head === 'function' ? r2.head(`${dir}/${file}.parquet`) : Promise.resolve(true)])
    if (!data) throw Object.assign(new Error(`${dir}/${file}.parquet: not found`), { name: 'NotFoundError' })
    return footer
  }, 30_000, ivJoin(env, 'run-footer'))
}

/** A handle the isolate shares, as `env`'s request reads it: its reads (and its runs') are that request's work
 *  (`reqOf`: what it joins, what it owns and what its cancel evicts), not the work of the request that opened it. */
function ivOwn(env: Env, h: IndexHandle): IndexHandle {
  if (reqOf(h.env) === reqOf(env)) return h
  return { ...h, env, ...(h.runs ? { runs: h.runs.map(r => ({ ...r, env })) } : {}) } as IndexHandle
}

/** A served sort of the interval store as an index handle as of `date`'s scan (`pq` mode over its
 *  `.groups.parquet`, bytes from `INDEX_R2`); null when the store doesn't hold that date or sort. Under
 *  `slices(env)` the variant reads its owner-slice sort (`IV_SLICE_SORTS`; the handle keeps the variant's
 *  name, so lens-sorted logic holds); a held date's other variants (a v1 `user` sort) are refused, so a
 *  sliced read of a held date never mixes in per-scan rows. A generation without slice sorts: null. */
export async function openInterval(env: Env, date: string, variant: string, retried = false): Promise<IndexHandle | null> {
  const sliced = isSliced(env)
  const file0 = sliced ? IV_SLICE_SORTS.get(variant) : IV_SORTS.has(variant) ? variant : undefined
  // The sort's footer (and its sibling's: a view reads P's row from `path`, then `bysize`, and a diff
  // both) starts loading now, beside `scans.json`, not after it.
  if (file0 && !(sliced && ivNoSlices.has(env.INTERVAL_STORE_GEN!))) for (const f of [file0, IV_SIBLING.get(file0)]) if (f) ivFooter(env, f).catch(() => {})
  if (sliced && !file0 && (variant === 'user' || variant.startsWith('coarse'))) {
    const held = (await ivScans(env)).has(date)
    if (held && !ivNoSlices.has(env.INTERVAL_STORE_GEN!)) throw new Error(`index variant '${variant}' not synced for ${date}`)
  }
  if (!file0) return null
  const st = await ivState(env)
  const asOf = st.scans.get(date)
  if (asOf == null) return null
  // The standalone `reads` sort has no runs: it answers the base's scans only.
  if (variant === 'reads' && !st.base.has(date)) return null
  // A request naming its scans (`asOfScans`) reads the runs that begin by the newest of them (and `date`).
  const named = (env as Env & { [IV_ASOF]?: string[] })[IV_ASOF]
  const horizon = named ? Math.max(asOf, ...named.map(d => st.scans.get(d) ?? Infinity)) : Infinity
  const runs = variant === 'reads' ? [] : st.runs.filter(r => st.scans.get(r.first)! <= horizon)
  const gen = env.INTERVAL_STORE_GEN!
  if (sliced) {
    const at = ivNoSlices.get(gen)
    if (at != null && Date.now() - at < 60_000) return null
  }
  for (const r of runs) for (const f of [file0, IV_SIBLING.get(file0)]) if (f) ivRunFooter(env, r.key, f).catch(() => {})
  const ck = `iv:${gen}${ivRev(env)}:${date}:${variant}${sliced ? ':s' : ''}|${runs.map(r => r.key).join(',')}`
  try {
    return ivOwn(env, await shared(handles, ck, async (): Promise<IndexHandle> => {
      const src = r2Bytes(env.INDEX_R2!)
      const dir = `${IV_PREFIX}/${gen}/served`
      const key = ivKey(env, `${dir}/${file0}.parquet`)
      const footer = await ivFooter(env, file0)
      const kv = new Map((footer.metadata.key_value_metadata ?? []).map(e => [e.key, e.value]))
      const schema = JSON.parse(kv.get('schema')!) as SchemaElement[]
      const version = Number(kv.get('version'))
      const file: FileSlice = { get byteLength() { return 0 }, slice: async (s0, e) => toBuffer((await src.get(key, { offset: s0, length: (e ?? s0) - s0 })).bytes) }
      const h: PqHandle = { mode: 'pq', file, env, date, variant, gen: `iv:${gen}${ivRev(env)}${sliced ? ':s' : ''}`, dir, schema, version, columns: variant === 'reads' ? null : rowColumns(version, schema), floor: null, footer, asOf, src, dataKey: key }
      if (!runs.length) return h
      return { ...h, runs: await Promise.all(runs.map(r => openIvRun(env, h, r.key, file0))) }
    }, 300_000, ivJoin(env, 'handle')))
  } catch (e) {
    // A run that won't open is cut out; the date is then the store's (an older run's or the base's) or per-scan.
    if (e instanceof IvTierBroken && !retried) return openInterval(env, date, variant, true)
    if (!sliced || (e as Error).name !== 'NotFoundError') throw e
    ivNoSlices.set(gen, Date.now())
    return null
  }
}

/** Run `run`'s handle on sort `file`, as of `base`'s scan (`IvTierBroken` when its files won't open). */
async function openIvRun(env: Env, base: PqHandle, run: string, file: string): Promise<IndexHandle> {
  const dir = `${IV_PREFIX}/${env.INTERVAL_STORE_GEN!}/${run}/served`
  const key = ivKey(env, `${dir}/${file}.parquet`)
  let footer: FooterIndex
  try {
    footer = await ivRunFooter(env, run, file)
  } catch (e) {
    throw ivMarkBroken(env, run, e)
  }
  const kv = new Map((footer.metadata.key_value_metadata ?? []).map(e => [e.key, e.value]))
  const schema = JSON.parse(kv.get('schema')!) as SchemaElement[]
  const version = Number(kv.get('version'))
  const src = base.src!
  const fileSlice: FileSlice = { get byteLength() { return 0 }, slice: async (s0, e) => toBuffer((await src.get(key, { offset: s0, length: (e ?? s0) - s0 })).bytes) }
  return { ...base, file: fileSlice, gen: `${base.gen}/${run}`, dir, schema, version, columns: rowColumns(version, schema), footer, run, runs: undefined, dataKey: key }
}

/** Each path's `last_read` at a scan, from the interval store's `reads` sort (absent = never read): per
 *  depth, the groups whose path range holds one of the asked paths, read raw (the sort's rows aren't
 *  `Row`s) and tested live at the scan. A view's few thousand tiles cost the groups that hold them. */
export async function ivLastRead(env: Env, date: string, paths: string[], maxGroups = 400): Promise<Map<string, number>> {
  const h = await openInterval(env, date, 'reads')
  const out = new Map<string, number>()
  if (!h || !paths.length) return out
  const D = h.asOf!
  const byDepth = new Map<number, string[]>()
  for (const p of paths) {
    const d = p === '' ? 0 : p.split('/').length
    const l = byDepth.get(d)
    if (l) l.push(p)
    else byDepth.set(d, [p])
  }
  const found = new Map<number, Span>()
  await mapLimit([...byDepth.entries()], SPAN_QUERIES, async ([depth, ps]) => {
    ps.sort()
    const cand = await selectSpans(h, [{ dLo: depth, dHi: depth, pLo: ps[0], pHi: ps[ps.length - 1] }], 1 << 20)
    for (const g of cand) {
      if (g.dMin !== g.dMax) { found.set(g.rg, g); continue }
      // the first asked path at or past the group's lowest: inside its range?
      let lo = 0, hi = ps.length
      while (lo < hi) { const m = (lo + hi) >> 1; if (ps[m] < g.pMin) lo = m + 1; else hi = m }
      if (lo < ps.length && ps[lo] <= g.pMax) found.set(g.rg, g)
    }
  })
  const spans = [...found.values()].sort((x, y) => x.rg - y.rg)
  if (spans.length > maxGroups) throw new Error(`read-day lookup too wide: ${spans.length} row groups (cap ${maxGroups})`)
  const jsons = await fetchGroupJson(h, spans.map(x => x.rg))
  const want = new Set(paths)
  const cols = ['depth', 'path', 'vf', 'vt', 'last_read']
  await mapLimit(spans, GROUP_READS, async x => {
    const j = jsons.get(x.rg)
    if (!j) return
    for (const r of await readGroupRaw(h, j, cols)) {
      if (num(r.vf) <= D && D < num(r.vt) && want.has(str(r.path))) out.set(str(r.path), num(r.last_read))
    }
  })
  return out
}

/** The physical columns a `Row` decodes from, per generation — the wire
 * names on a v1 index, the layer-2's on a store sort (§1.1). `usr` and the
 * class pivots only where the file has them (cw has neither). */
export const V1_ROW_COLUMNS = ['path', 'depth', 'usr', 'b', 'o', 'wts', 'wb', 'c2', 'c3', 'c4', 'a']
export const IV_ROW_COLUMNS = ['depth', 'path', 'usr', 'vf', 'vt', 'kind', 'size', 'n_files', 'n_children', 'n_desc', 'mtime', 'wts', 'wb', 'c2', 'c3', 'c4', 'us', 'last_read']
export const V2_ROW_COLUMNS = ['path', 'depth', 'usr', 'kind', 'size', 'n_files', 'n_children', 'n_desc', 'mtime', 'mtime_mean', 'last_read', 'sum_storage_class_id_2', 'sum_storage_class_id_3', 'sum_storage_class_id_4', ...AGE_COLS]

/** What a shaped read projects: a v1 index reads every column (its columns
 * are the row); a store sort reads the `Row` columns it has, so a bridge
 * generation's wire aliases and `created` are never fetched or decoded. */
export function rowColumns(version: number, schema: SchemaElement[]): string[] | null {
  if (version < 2) return null
  if (version >= 3) {
    const have = new Set(schema.slice(1).map(l => l.name))
    return IV_ROW_COLUMNS.filter(c => have.has(c))
  }
  const have = new Set(schema.slice(1).map(l => l.name))
  return V2_ROW_COLUMNS.filter(c => have.has(c))
}

/** The columns a caller's projection must name on this handle's generation,
 * for reads that pick a subset (the owner totals): the `Row` field → column. */
export function columnsFor(h: IndexHandle, fields: (keyof Row)[]): string[] {
  const v2: Partial<Record<keyof Row, string[]>> = { size: ['size'], n_files: ['n_files'], last_read: ['last_read'], cls2: ['sum_storage_class_id_2'], cls3: ['sum_storage_class_id_3'], cls4: ['sum_storage_class_id_4'], mtime_mean: ['mtime_mean', 'size'], mtime_w: ['mtime_mean', 'size'] }
  const v1: Partial<Record<keyof Row, string[]>> = { size: ['b'], n_files: ['o'], last_read: ['a'], cls2: ['c2'], cls3: ['c3'], cls4: ['c4'], mtime_mean: ['wts', 'wb'], mtime_w: ['wb'] }
  const v3: Partial<Record<keyof Row, string[]>> = { size: ['size'], n_files: ['n_files'], last_read: ['last_read'], cls2: ['c2'], cls3: ['c3'], cls4: ['c4'], mtime_mean: ['wts', 'wb'], mtime_w: ['wb'], us: ['us', 'size'] }
  const map = h.version >= 3 ? v3 : isStore(h) ? v2 : v1
  const have = new Set(h.schema.slice(1).map(l => l.name))
  const out = new Set<string>()
  for (const f of fields) for (const c of map[f] ?? [f]) if (have.has(c)) out.add(c)
  // An interval store's rows are versions: a read keeps those live at its scan, by `vf`/`vt` — and with runs, combines
  // a version's rows across tiers by its identity.
  if (h.asOf != null) for (const c of ['vf', 'vt']) out.add(c)
  if (h.runs?.length) for (const c of ['depth', 'path', 'usr']) if (have.has(c)) out.add(c)
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
/** The footer columns a span predicate reads (all but the metadata). */
const FOOTER_BOUNDS = FOOTER_COLS.filter(c => c !== 'rg_json')

/** The first tail read of a `.groups.parquet`: its whole footer in one
 * request up to ~170 footer groups (~1.5 KB of thrift each, measured on cw's
 * `bysize`: 14 groups → 20.8 KB) — a gcs sort at 32K-row tier groups has
 * ~47; a larger footer costs one more request. */
const FOOTER_TAIL = 1 << 18
/** …and with a suffix read (`ByteStore.tail`): an interval store's whole footer (~0.5 MB for 276 footer
 *  groups, gcs `2026-10-09b`) in the one request. */
const FOOTER_TAIL_SUFFIX = 1 << 20

const colo = (): Cache => (caches as unknown as { default: Cache }).default
/** A colo-cache key for part of a store object — a secondary store's under its own segment. */
const coloKey = (env: Env, key: string, part: string): Request => {
  const st = storeKey(env)
  return new Request(`https://index-footer.cache/${st === PRIMARY_STORE ? '' : `@${st}/`}${key}?${part}`)
}
const toBuffer = (b: Uint8Array): ArrayBuffer => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer

/** Bytes `[start, end)` of a store object through the colo cache (a
 * generation dir is immutable, so a range is too). */
export async function cachedRange(env: Env, key: string, start: number, end: number, src: ByteStore = makeStore(env)): Promise<ArrayBuffer> {
  const ck = coloKey(env, key, `r=${start}-${end}`)
  const hit = await colo().match(ck)
  if (hit) return hit.arrayBuffer()
  const buf = toBuffer((await src.get(key, { offset: start, length: end - start })).bytes)
  await colo().put(ck, new Response(buf, { headers: { 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
  return buf
}

/** A small parquet's footer bytes (metadata + the 8-byte trailer), colo-
 * cached: one tail read of `FOOTER_TAIL` bytes, one more when the footer is
 * longer. Throws the store's `NotFoundError` when the file is absent. */
export async function readFooterBytes(env: Env, key: string, src?: ByteStore): Promise<ArrayBuffer> {
  const ck = coloKey(env, key, 'footer')
  let buf: ArrayBuffer
  const hit = await colo().match(ck)
  if (hit) buf = await hit.arrayBuffer()
  else {
    const store = src ?? makeStore(env)
    const suffix = src?.tail?.bind(src)
    let size: number | undefined
    let tail: Uint8Array
    if (suffix) {
      // One request: the last `FOOTER_TAIL_SUFFIX` bytes and the object's size (an interval store's
      // footer is ~0.5 MB: a size probe, a tail and a head read would be three round trips in series).
      const got = await suffix(key, FOOTER_TAIL_SUFFIX)
      size = got.totalSize
      tail = new Uint8Array(toBuffer(got.bytes))
    } else {
      size = (await store.get(key, { offset: 0, length: 1 })).totalSize
      if (size == null || !(size >= 12)) throw new Error(`${key}: size unknown or too small (${size})`)
      const at = Math.max(0, size - FOOTER_TAIL)
      tail = new Uint8Array(toBuffer((await store.get(key, { offset: at, length: size - at })).bytes))
    }
    if (size == null || !(size >= 12)) throw new Error(`${key}: size unknown or too small (${size})`)
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
async function openFooter(env: Env, key: string, src?: ByteStore): Promise<FooterIndex> {
  const metadata = parquetMetadata(await readFooterBytes(env, key, src))
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
    // An interval store's footer adds the groups' time bounds; missing stats leave the group a candidate.
    const time = cols.has('vf_min') && lo('vf_min') != null && hi('vt_max') != null ? { vfMin: num(lo('vf_min')), vtMax: num(hi('vt_max')) } : {}
    const bounds = req.some(v => v == null) || !uKnown
      ? null
      : { dMin: num(req[0]), dMax: num(req[1]), pMin: str(req[2]), pMax: str(req[3]), bMax: num(req[4]), uMin: lo('u_min') == null ? null : str(lo('u_min')), uMax: hi('u_max') == null ? null : str(hi('u_max')), ...time }
    const rows = Number(meta.num_rows)
    const g = { n, rgStart: rg, rgEnd: rg + rows, byteStart, byteEnd, meta, bounds }
    rg += rows
    return g
  })
  return { key, metadata, groups }
}

/** The byte span of some columns' chunks in a footer group (they're stored in schema order, so a
 *  run of columns is one range). */
function chunksSpan(fg: FooterGroup, cols: Set<string>): [number, number] {
  let lo = Infinity, hi = 0
  for (const c of fg.meta.columns) {
    const m = c.meta_data!
    if (!cols.has(m.path_in_schema[0])) continue
    const data = Number(m.data_page_offset)
    const dict = m.dictionary_page_offset == null ? data : Number(m.dictionary_page_offset)
    const start = dict > 0 ? Math.min(dict, data) : data
    lo = Math.min(lo, start)
    hi = Math.max(hi, start + Number(m.total_compressed_size))
  }
  return [lo, hi]
}

/** Decode some columns of one per-scan footer group (one range read of their chunks, colo-cached): the
 *  columns asked (its bounds are a fifth of the bytes). An interval store's are read whole, `ivFooterDoc`. */
async function readFooterCols(h: PqHandle, fg: FooterGroup, columns: string[]): Promise<Record<string, unknown>[]> {
  const [start, end] = chunksSpan(fg, new Set(columns))
  const buf = await cachedRange(h.env, h.footer.key, start, end, h.src)
  const file: FileSlice = { byteLength: end, slice: async (s, e) => buf.slice(s - start, (e ?? end) - start) }
  const metadata = { ...h.footer.metadata, row_groups: [fg.meta], num_rows: fg.meta.num_rows }
  return (await parquetReadObjects({ file, metadata, columns, compressors })) as Record<string, unknown>[]
}

// --- the interval store's footers, pre-decoded through the colo cache ---------------------------------
//
// A Worker's CPU decodes parquet several times slower than a laptop's (~100 ms per 8K-row group, 2026-09-15), and
// a new isolate opens every footer it reads: an interval store's sort footer (~0.5 MB of thrift, 276 footer
// groups) and each run's, then tens of footer groups (bounds and `rg_json`, ~95 KB each). Each is immutable per
// generation (and revision), so each is decoded once per colo and held there as JSON, which a new isolate parses
// natively: the footer as its groups' byte ranges and bounds (~40 KB), a footer group as its columns.

/** A footer as `openIvFooter` holds it in the colo. */
interface CompactFooter { kv: { key: string; value?: string }[]; groups: [number, number, number, number, number, FooterGroup['bounds']][] }

/** An interval store's footer (`openFooter`'s shape, compact: `FooterIndex.full`), from the colo's decoded copy;
 *  on a miss parsed whole and the copy put. */
async function openIvFooter(env: Env, key: string, src: ByteStore): Promise<FooterIndex> {
  const ck = coloKey(env, key, 'compact=1')
  let whole: Promise<FooterIndex> | undefined
  const full = () => (whole ??= openFooter(env, key, src))
  const hit = await colo().match(ck)
  if (hit) {
    const d = await hit.json<CompactFooter>()
    const metadata = { key_value_metadata: d.kv } as FileMetaData
    const groups = d.groups.map(([n, rgStart, rgEnd, byteStart, byteEnd, bounds]): FooterGroup => ({ n, rgStart, rgEnd, byteStart, byteEnd, bounds, meta: undefined as unknown as RowGroup }))
    return { key, metadata, groups, full }
  }
  const f = await full()
  const doc: CompactFooter = { kv: f.metadata.key_value_metadata ?? [], groups: f.groups.map(g => [g.n, g.rgStart, g.rgEnd, g.byteStart, g.byteEnd, g.bounds]) }
  await colo().put(ck, new Response(JSON.stringify(doc), { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
  return { ...f, full }
}

/** The columns of an interval store's footer group (bounds, time bounds, metadata, then the size floor and segment). */
const IV_FOOTER_COLS = [...FOOTER_BOUNDS, 'vf_min', 'vf_max', 'vt_min', 'vt_max', 'rg_json', 'b_min', 'seg']
/** The colo copy's layout (`IV_FOOTER_COLS`): bumped when its columns change, so an older copy is never read. */
const IV_FOOTER_DOC = 2
/** A footer group as the colo holds it: one array per column of `IV_FOOTER_COLS`, in its rows' (`rg`) order. */
type FooterDoc = Record<string, (number | string | null)[]>
const docsInFlight = new Map<string, Promise<{ groups: BlobGroup[]; json: [number, string][] }>>()

/** One footer group of an interval store, whole — its tier groups' bounds and their metadata — once per isolate
 *  (the two halves seeded into the footer LRU as `fg:<n>` and `fj:<n>`, read back by `readFooterGroup` /
 *  `readFooterJson`), from the colo's decoded copy, or on a miss fetched, decoded and put there. A read that
 *  plans on a group's bounds then wants its `rg_json`: reading the two apart would cost a round trip each. */
async function ivFooterDoc(h: PqHandle, fg: FooterGroup): Promise<{ groups: BlobGroup[]; json: [number, string][] }> {
  const fk = (part: string) => `${storeKey(h.env)}|iv|${h.variant}|${h.gen}|${part}`
  return inFlight(docsInFlight, fk(`fd:${fg.n}`), async () => {
    const t0 = now()
    const ck = coloKey(h.env, h.footer.key, `fd=${fg.n}&d=${IV_FOOTER_DOC}`)
    const hit = await colo().match(ck)
    let doc: FooterDoc
    if (hit) doc = await hit.json<FooterDoc>()
    else {
      const full = h.footer.full ? await h.footer.full() : h.footer
      const ffg = full.groups[fg.n]
      const buf = toBuffer((await h.src!.get(h.footer.key, { offset: ffg.byteStart, length: ffg.byteEnd - ffg.byteStart })).bytes)
      const file: FileSlice = { byteLength: ffg.byteEnd, slice: async (s, e) => buf.slice(s - ffg.byteStart, (e ?? ffg.byteEnd) - ffg.byteStart) }
      const metadata = { ...full.metadata, row_groups: [ffg.meta], num_rows: ffg.meta.num_rows }
      const rows = (await parquetReadObjects({ file, metadata, columns: IV_FOOTER_COLS, compressors })) as Record<string, unknown>[]
      doc = Object.fromEntries(IV_FOOTER_COLS.map(c => [c, rows.map(r => r[c] == null ? null : typeof r[c] === 'string' ? r[c] as string : num(r[c]))]))
      await colo().put(ck, new Response(JSON.stringify(doc), { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${BLOB_CACHE_TTL}` } }))
    }
    const col = (c: string) => doc[c] ?? []
    const [rg, d0, d1, p0, p1, bx, u0, u1, rs, re, vf0, vf1, vt0, vt1, js, bn, sg] = IV_FOOTER_COLS.map(col)
    const groups = rg.map((_, i): BlobGroup => ({
      rg: rg[i] as number, dMin: d0[i] as number, dMax: d1[i] as number, pMin: p0[i] as string, pMax: p1[i] as string, bMax: bx[i] as number,
      uMin: u0[i] as string | null, uMax: u1[i] as string | null, rowStart: rs[i] as number, rowEnd: re[i] as number, rgJson: '',
      vfMin: vf0[i] as number, vfMax: vf1[i] as number, vtMin: vt0[i] as number, vtMax: vt1[i] as number,
      ...(bn[i] != null && sg[i] != null ? { bMin: bn[i] as number, seg: sg[i] as number } : {}),
    }))
    // Footer rows are in `rg` order from the group's first.
    const json = js.map((j, i): [number, string] => [fg.rgStart + i, j as string])
    h.trace?.('footer', now() - t0, h.variant)
    footerCache.put(fk(`fg:${fg.n}`), groups, groupsBytes(groups))
    footerCache.put(fk(`fj:${fg.n}`), json, jsonBytes(json))
    return { groups, json }
  }, ivJoin(h.env, 'footer-doc'))
}
const groupsBytes = (gs: BlobGroup[]): number => gs.reduce((n, g) => n + 96 + 2 * (g.pMin.length + g.pMax.length), 0)
const jsonBytes = (js: [number, string][]): number => js.reduce((n, [, j]) => n + 32 + 2 * j.length, 0)

/** Decode one footer group's bounds into the blob handle's group shape (`rgJson` left empty: the
 * metadata of the few groups a read selects is fetched apart, `readFooterJson`) — cached per isolate
 * beside the tier groups, `(store, date, variant, gen, fg:<n>)`. Without the metadata column a
 * footer group is a fifth of the bytes to fetch, decode and hold. */
async function readFooterGroup(h: PqHandle, fg: FooterGroup): Promise<BlobGroup[]> {
  return footerOnce(h, `fg:${fg.n}`, async () => {
    if (h.asOf != null) {
      const { groups } = await ivFooterDoc(h, fg)
      return [groups, groupsBytes(groups)]
    }
    const t0 = now()
    const rows = await readFooterCols(h, fg, FOOTER_BOUNDS)
    const out = rows.map((r): BlobGroup => ({
      rg: num(r.rg), dMin: num(r.d_min), dMax: num(r.d_max), pMin: str(r.p_min), pMax: str(r.p_max), bMax: num(r.b_max),
      uMin: r.u_min == null ? null : str(r.u_min), uMax: r.u_max == null ? null : str(r.u_max),
      rowStart: num(r.row_start), rowEnd: num(r.row_end), rgJson: '',
    }))
    h.trace?.('footer', now() - t0, h.variant)
    return [out, groupsBytes(out)]
  })
}

/** Footer decodes in flight (`footerOnce`): concurrent reads of one footer group share its decode. */
const footersInFlight = new Map<string, Promise<unknown[]>>()

/** A footer group's decode (`part`), cached per isolate and shared while in flight. Keyed per scan for a
 *  per-scan store; an interval store's footer serves every scan, so its entries are keyed without one. */
async function footerOnce<T>(h: PqHandle, part: string, make: () => Promise<[T[], number]>): Promise<T[]> {
  const k = `${storeKey(h.env)}|${h.asOf != null ? 'iv' : h.date}|${h.variant}|${h.gen}|${part}`
  const hit = footerCache.get<T>(k)
  if (hit) return hit
  return inFlight(footersInFlight, k, () => make().then(([v, bytes]) => { footerCache.put(k, v, bytes); return v as unknown[] }), ivJoin(h.env, 'footer-group')) as Promise<T[]>
}

/** The stored metadata (`rg_json`) of one footer group's tier groups, by `rg` (cached per isolate). */
async function readFooterJson(h: PqHandle, fg: FooterGroup): Promise<Map<number, string>> {
  return new Map(await footerOnce<[number, string]>(h, `fj:${fg.n}`, async () => {
    if (h.asOf != null) {
      const { json } = await ivFooterDoc(h, fg)
      return [json, jsonBytes(json)]
    }
    const t0 = now()
    // Footer rows are in `rg` order from the group's first: `rg_json` alone is one chunk to fetch.
    const rows = await readFooterCols(h, fg, ['rg_json'])
    const out = rows.map((r, i): [number, string] => [fg.rgStart + i, str(r.rg_json)])
    h.trace?.('fjson', now() - t0, h.variant)
    return [out, jsonBytes(out)]
  }))
}

/** The tier groups of a `pq` handle that `pass` (the span predicate)
 * accepts: footer groups whose bounds fail it are never fetched or decoded.
 * Sound because each predicate is monotone in the bounds — a footer group's
 * bounds contain every row's — and `groupMatches` / `groupMatchesSize`
 * applied to the bounds is exactly that relaxation (a depth-spanning range
 * skips the path test, a mixed-user one the keyed rect). */
async function pqGroups(h: PqHandle, pass0: (g: NonNullable<FooterGroup['bounds']>) => boolean, near?: (g: BlobGroup, prev?: BlobGroup, next?: BlobGroup) => boolean): Promise<BlobGroup[]> {
  // As of a scan (an interval store): a group can answer only if one of its versions is live then.
  // A run's group can matter once a version in it opened by then, live or not (its close records end versions).
  const D = h.asOf
  const run = !!h.run
  const pass = D == null ? pass0 : (g: { vfMin?: number; vtMax?: number } & NonNullable<FooterGroup['bounds']>) =>
    (g.vfMin == null || g.vfMin <= D) && (run || g.vtMax == null || D < g.vtMax) && pass0(g)
  const sel = h.footer.groups.filter(fg => !fg.bounds || pass(fg.bounds))
  h.trace?.('fgroups', sel.length)
  const all = (await mapLimit(sel, GROUP_READS, fg => readFooterGroup(h, fg))).flat()
  if (!near) return all.filter(pass)
  // `all` is in `rg` order, a footer group's tier groups after the previous one's: a group's neighbours in the file
  // are those next to it here with the adjacent `rg` (an unselected footer group's are not here, and not used).
  return all.filter((g, i) => {
    if (!pass(g)) return false
    const prev = i > 0 && all[i - 1].rg === g.rg - 1 ? all[i - 1] : undefined
    const next = i + 1 < all.length && all[i + 1].rg === g.rg + 1 ? all[i + 1] : undefined
    return near(g, prev, next)
  })
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

/** An interval-store row (`interval_store.PV_SCHEMA`): one per path, owner slices in `us`, the mean
 *  stamp as its weighted sum (`wts`) and weight (`wb`), `-1` where a v1 scan had no structural counts. */
const toRowV3 = (r: Record<string, unknown>): Row => {
  const wb = num(r.wb)
  const size = num(r.size)
  const opt = (v: unknown) => { const n = num(v); return n < 0 ? null : n }
  return {
    path: str(r.path),
    depth: num(r.depth),
    // A slice sort's row is one owner's slice (`usr`); a path sort's folds them into `us`.
    usr: r.usr == null || r.usr === '' ? null : str(r.usr),
    kind: str(r.kind) === 'file' ? 'file' : 'dir',
    size,
    n_files: num(r.n_files),
    n_children: opt(r.n_children),
    n_desc: opt(r.n_desc),
    mtime: opt(r.mtime),
    mtime_mean: wb > 0 ? num(r.wts) / wb : null,
    mtime_w: wb,
    last_read: r.last_read == null ? null : opt(r.last_read),
    cls2: num(r.c2),
    cls3: num(r.c3),
    cls4: num(r.c4),
    us: r.us == null ? null : usMap(str(r.us), size),
    ages: null,
    vf: num(r.vf),
    vt: num(r.vt),
  }
}

/** `us` (`interval_store.us_sql_from_raw`): `''` none, a bare name (the whole path), else JSON pairs. */
export function usMap(us: string, size: number): Record<string, number> | null {
  if (!us) return null
  if (us[0] !== '[') return { [us]: size }
  return Object.fromEntries(JSON.parse(us) as [string, number][])
}

/** The decoder for a handle's generation. */
export const toRow = (h: { version: number }): ((r: Record<string, unknown>) => Row) => (h.version >= 3 ? toRowV3 : h.version >= 2 ? toRowV2 : toRowV1)

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
  const raw = await readGroupRaw(h, rgJson, columns ?? h.columns ?? undefined, held)
  const D = h.asOf
  return (D == null ? raw : raw.filter(r => num(r.vf) <= D && D < num(r.vt))).map(toRow(h))
}

/** One row group's rows, every version (an interval-store handle's tier: `combineLive` tests them after combining). */
async function readGroupVersions(h: IndexHandle, rgJson: string, columns?: string[]): Promise<Row[]> {
  return (await readGroupRaw(h, rgJson, columns ?? h.columns ?? undefined)).map(toRow(h))
}


// --- an interval store's decoded row groups, through the colo cache ------------------------------------
//
// Decoding a row group is the cold read's CPU: ~25 ms on a laptop for an 8K-row group of the interval store, four
// fifths of it zstd (`fzstd`, pure JS: Workers compile no wasm at runtime), and several times that on a Worker. An
// interval store's group serves every scan (`ivGroupKey`) and never changes, so the first isolate in a colo to
// decode it holds it there as gzipped columnar JSON (~2 MB, ~0.3 MB stored; inflated and parsed natively in a few
// ms): every later isolate reads that instead of the range and its decode.

/** A decoded group as the colo holds it: its columns, each an array over its rows. */
type GroupDoc = { cols: string[]; v: unknown[][] }

const groupColoKey = (h: IndexHandle, rg: number): Request => coloKey(h.env, h.dataKey!, `rg=${rg}`)

/** `raw` (a group's decoded columns) as a `GroupDoc`, or null when a value wouldn't survive JSON exactly (an
 *  integer past 2^53, a non-finite float, a byte array): such a group is decoded from parquet every time. */
export function groupDoc(raw: Record<string, unknown>[], cols: string[]): GroupDoc | null {
  const v: unknown[][] = cols.map(() => new Array(raw.length))
  for (let i = 0; i < raw.length; i++) {
    const r = raw[i]
    for (let j = 0; j < cols.length; j++) {
      let x = r[cols[j]]
      if (typeof x === 'bigint') {
        if (x > BigInt(Number.MAX_SAFE_INTEGER) || x < -BigInt(Number.MAX_SAFE_INTEGER)) return null
        x = Number(x)
      } else if (typeof x === 'number') {
        if (!Number.isFinite(x)) return null
      } else if (x === undefined) x = null
      else if (x !== null && typeof x !== 'string' && typeof x !== 'boolean') return null
      v[j][i] = x
    }
  }
  return { cols, v }
}

/** A `GroupDoc` back as raw column records (`readGroupRaw`'s shape, every int as a number). */
export function groupRows(d: GroupDoc): Record<string, unknown>[] {
  const n = d.v[0]?.length ?? 0
  const out: Record<string, unknown>[] = new Array(n)
  for (let i = 0; i < n; i++) {
    const r: Record<string, unknown> = {}
    for (let j = 0; j < d.cols.length; j++) r[d.cols[j]] = d.v[j][i]
    out[i] = r
  }
  return out
}

/** A read's cheap test of a row from its raw `depth`, `path` and `size`, true for every row its `keep` holds (and
 *  maybe more): an interval store's group shapes only the rows it passes (`IvGroup.pick`). */
export type Pre = (depth: number, path: string, size: number) => boolean

/** An interval store's group as the isolate holds it, every version: its decoded columns, each row shaped only
 *  when a read wants it. A drilled view keeps a few of a size sort's 8K rows per group, and shaping all of them
 *  (`groupRows` + `toRow`) cost more than parsing them. `all` shapes every row once; `pick` the rows a read's
 *  `Pre` passes, without the rest. A group whose values JSON can't hold (`groupDoc` null) is held shaped. */
export class IvGroup {
  private rows?: Row[]
  constructor(private readonly h: { version: number }, private readonly doc: GroupDoc | null, rows?: Row[]) {
    this.rows = rows
  }
  get length(): number {
    return this.rows?.length ?? this.doc?.v[0]?.length ?? 0
  }
  all(): Row[] {
    return (this.rows ??= groupRows(this.doc!).map(toRow(this.h)))
  }
  pick(pre: Pre): Row[] {
    if (this.rows) return this.rows.filter(r => pre(r.depth, r.path, r.size))
    const { cols, v } = this.doc!
    const [d, p, s] = ['depth', 'path', 'size'].map(c => cols.indexOf(c))
    if (d < 0 || p < 0 || s < 0) return this.all()
    const shape = toRow(this.h)
    const [dv, pv, sv] = [v[d], v[p], v[s]]
    const out: Row[] = []
    for (let i = 0; i < pv.length; i++) {
      if (!pre(dv[i] as number, pv[i] as string, sv[i] as number)) continue
      const r: Record<string, unknown> = {}
      for (let j = 0; j < cols.length; j++) r[cols[j]] = v[j][i]
      out.push(shape(r))
    }
    return out
  }
}

/** The colo's decoded copy of an interval store's group (null: not there). */
async function coloGroup(h: IndexHandle, rg: number): Promise<IvGroup | null> {
  const hit = await colo().match(groupColoKey(h, rg))
  if (!hit?.body) return null
  const t0 = now()
  const d = await new Response(hit.body.pipeThrough(new DecompressionStream('gzip'))).json<GroupDoc>()
  h.trace?.('gjson', now() - t0, h.variant)
  return new IvGroup(h, d)
}

/** Put a group's decoded copy in the colo, best effort and not waited for (a read never waits on the cache). */
function putColoGroup(h: IndexHandle, rg: number, d: GroupDoc): void {
  const body = new Response(JSON.stringify(d)).body!.pipeThrough(new CompressionStream('gzip'))
  // Buffered: the Cache API wants a known length.
  const p: Promise<void> = new Response(body).arrayBuffer()
    .then(buf => colo().put(groupColoKey(h, rg), new Response(buf, { headers: { 'content-type': 'application/octet-stream', 'cache-control': `max-age=${BLOB_CACHE_TTL}` } })))
    .catch(() => {})
    .finally(() => { coloPuts.delete(p) })
  coloPuts.add(p)
}
/** Decoded row groups being put in the colo (`putColoGroup`). */
const coloPuts = new Set<Promise<void>>()
/** Resolves once every colo put of a decoded row group started so far has landed (or failed). */
export const coloPutsSettled = async (): Promise<void> => { await Promise.all([...coloPuts]) }

/** The versions of `rows` live at the handle's scan (all of them for a per-scan handle). */
const liveAt = (h: IndexHandle, rows: Row[]): Row[] => {
  const D = h.asOf
  return D == null ? rows : rows.filter(r => r.vf! <= D && D < r.vt!)
}

export interface Span extends GroupSpan {
  rg: number
  /** The tier the group is in, on a handle with `runs`: 0 (or absent) its own file, `i` its run `i - 1`. */
  tier?: number
}

/** A span's identity across tiers. */
const spanKey = (s: Span): number => (s.tier ?? 0) * 2 ** 32 + s.rg
const bySpan = (a: Span, b: Span): number => (a.tier ?? 0) - (b.tier ?? 0) || a.rg - b.rg
/** A handle's tiers: itself, then its runs. */
const tiersOf = (h: IndexHandle): IndexHandle[] => [h, ...(h.runs ?? [])]

/** Rows of every tier combined, one per version (`(depth, path, usr, vf)`; the smallest `vt`), then those live at
 *  the handle's scan: `interval_read.combine` + `live_rows`. */
export function combineLive(h: IndexHandle, rows: Row[]): Row[] {
  const best = new Map<string, Row>()
  for (const r of rows) {
    const k = `${r.depth}\0${r.path}\0${r.usr ?? ''}\0${r.vf}`
    const c = best.get(k)
    if (!c || r.vt! < c.vt!) best.set(k, r)
  }
  return liveAt(h, [...best.values()])
}

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
/** A cold footer's decoded groups (`…|fg:<n>` → `BlobGroup[]`) and their metadata (`…|fj:<n>`), in an
 *  LRU of their own: an interval store's reads touch tens of footer groups (~0.5 MB each with their
 *  `rg_json`), which in the rows' LRU evicted the very row groups a warm read wanted. */
const FOOTER_CACHE_CAP = 12 << 20

/** An LRU of decoded entries by an estimated byte size. */
class Lru {
  private m = new Map<string, { v: unknown[]; bytes: number }>()
  private bytes = 0
  constructor(private cap: number) {}
  get<T>(k: string): T[] | undefined {
    const hit = this.m.get(k)
    if (!hit) return undefined
    this.m.delete(k)
    this.m.set(k, hit) // most recent last
    return hit.v as T[]
  }
  put(k: string, v: unknown[], bytes: number): void {
    if (bytes > this.cap) return
    const old = this.m.get(k)
    if (old) {
      this.m.delete(k)
      this.bytes -= old.bytes
    }
    while (this.bytes + bytes > this.cap && this.m.size) {
      const [k0, e0] = this.m.entries().next().value as [string, { bytes: number }]
      this.m.delete(k0)
      this.bytes -= e0.bytes
    }
    this.m.set(k, { v, bytes })
    this.bytes += bytes
  }
}
/** Tier groups (`…|<rg>` → `Row[]`) and the search's decoded entries. */
const groupCache = new Lru(GROUP_CACHE_CAP)
const footerCache = new Lru(FOOTER_CACHE_CAP)

export function cacheGet<T>(k: string): T[] | undefined {
  return groupCache.get<T>(k)
}

export function cachePut(k: string, v: unknown[], bytes: number): void {
  groupCache.put(k, v, bytes)
}

const groupKey = (h: IndexHandle, rg: number) => `${storeKey(h.env)}|${h.date}|${h.variant}|${h.gen}|${rg}`
/** An interval-store group's key, without a scan: a group holds every scan's versions, so one decode
 *  serves every scan — both sides of a diff, a view and its lookups. It keys the group's cache entry
 *  (every version; `cachedGroup` keeps a read's scan's) and its fetch in flight, which a concurrent
 *  read of the same group waits for instead of fetching and decoding it again. One entry for both
 *  sides is what lets a root diff's ~20 groups fit the LRU (gcs `2026-10-09b`). */
const ivGroupKey = (h: IndexHandle, rg: number) => `${storeKey(h.env)}|iv|${h.variant}|${h.gen}|${rg}`
const groupsInFlight = new Map<string, Promise<IvGroup | null>>()

/** An interval store's group held by the isolate (every version), or undefined. */
const cachedIvGroup = (h: IndexHandle, rg: number): IvGroup | undefined => cacheGet<IvGroup>(ivGroupKey(h, rg))?.[0]

/** A group's cached rows at the handle's scan (an interval store's entry filtered to its live versions). */
function cachedGroup(h: IndexHandle, rg: number): Row[] | undefined {
  if (h.asOf == null) return cacheGet<Row>(groupKey(h, rg))
  const g = cachedIvGroup(h, rg)
  return g && liveAt(h, g.all())
}

/** Many row groups' shaped rows, each through the decoded-group cache; the
 * misses' projected chunks are fetched as merged range reads (`planRuns`:
 * neighbouring groups of a sort are adjacent in its file), `RUN_READS`
 * runs in flight, and each group decoded from its run's bytes. `f` maps each
 * group's rows (kept per group, in input order). */
/** `stop`: the caller no longer wants the answer (a time budget ran out) — groups not yet fetched or decoded
 *  are skipped (their slots stay empty), so abandoned work stops taking the isolate's CPU. */
/** `versions`: an interval store's groups are handed to `f` with every version (a tiered read combines the tiers'
 *  rows before testing liveness, `combineLive`), not only those live at the handle's scan. */
/** `pre`: on an interval store, the read's cheap row test (`Pre`); `f` is then handed only the rows it passes. */
async function readGroupsCached<T>(h: IndexHandle, groups: { rg: number; json: string }[], f: (rows: Row[]) => T, stop?: () => boolean, versions = false, pre?: Pre): Promise<T[]> {
  const out: T[] = new Array(groups.length)
  const miss: { i: number; rg: number; json: string; start: number; end: number }[] = []
  const waits: Promise<void>[] = []
  const iv = h.asOf != null
  // The groups this read fetches for any waiting read (`ivGroupKey` → resolve with every version).
  const mine = new Map<string, { resolve: (g: IvGroup | null) => void; p: Promise<IvGroup | null> }>()
  // A group's rows at this handle's scan (the entry holds every version: `cachedGroup`).
  const take = (i: number, rg: number, g: IvGroup, put: boolean) => {
    if (put) cachePut(ivGroupKey(h, rg), [g], g.length * ROW_BYTES)
    const rows = pre ? g.pick(pre) : g.all()
    out[i] = f(versions ? rows : liveAt(h, rows))
  }
  groups.forEach((g, i) => {
    const held = iv ? cachedIvGroup(h, g.rg) : undefined
    if (held) {
      h.trace?.('gcache', 1)
      take(i, g.rg, held, false)
      return
    }
    const hit = iv ? undefined : cachedGroup(h, g.rg)
    if (hit) {
      h.trace?.('gcache', 1)
      out[i] = f(hit)
      return
    }
    if (iv) {
      const fk = ivGroupKey(h, g.rg)
      const pending = groupsInFlight.get(fk)
      if (pending) {
        h.trace?.('gshared', 1)
        // Joined (`IV_JOIN`); a group its reader dropped (abandoned, failed, or frozen with its cancelled request) is
        // read here instead — an empty slot would be rows silently missing from the answer.
        waits.push(join(pending, ivJoin(h.env, 'group')).then(async got => {
          if (got !== STALLED && got) return take(i, g.rg, got, false)
          if (stop?.()) return
          out[i] = (await readGroupsCached(h, [g], f, stop, versions, pre))[0]
        }))
        return
      }
      let resolve!: (g: IvGroup | null) => void
      const p = new Promise<IvGroup | null>(r => { resolve = r })
      groupsInFlight.set(fk, p)
      track(p, ivJoin(h.env, 'group'), () => { if (groupsInFlight.get(fk) === p) groupsInFlight.delete(fk) })
      mine.set(fk, { resolve, p })
    }
    miss.push({ i, ...g, start: 0, end: 0 })
  })
  const settle = (fk: string, got: IvGroup | null) => {
    const r = mine.get(fk)
    if (!r) return
    mine.delete(fk)
    // Only this read's own entry: a joiner that evicted it may have put its own in place.
    if (groupsInFlight.get(fk) === r.p) groupsInFlight.delete(fk)
    r.resolve(got)
  }
  try {
    // An interval store's misses: first the colo's decoded copies (`coloGroup`), then the rest from the file.
    let fetch = miss
    if (iv && h.dataKey && miss.length) {
      // `RUN_READS` at once, as the range reads: each copy inflates to ~2 MB of JSON before it parses.
      const got = await mapLimit(miss, RUN_READS, g => (stop?.() ? Promise.resolve(null) : coloGroup(h, g.rg)))
      fetch = miss.filter((g, j) => {
        const all = got[j]
        if (!all) return true
        h.trace?.('gcolo', 1)
        take(g.i, g.rg, all, true)
        settle(ivGroupKey(h, g.rg), all)
        return false
      })
    }
    for (const g of fetch) [g.start, g.end] = chunkSpan(reviveRowGroup(g.json, h.schema) as unknown as Parameters<typeof chunkSpan>[0], h.columns ?? undefined)
    await mapLimit(planRuns(fetch), RUN_READS, async run => {
      if (stop?.()) return
      const file = bufferSlice(await fetchRange(h, run.start, run.end), run.start)
      for (const g of run.items) {
        if (stop?.()) return
        if (!iv) {
          const rows = await readGroup(h, g.json, undefined, file)
          cachePut(groupKey(h, g.rg), rows, rows.length * ROW_BYTES)
          out[g.i] = f(rows)
          continue
        }
        const raw = await readGroupRaw(h, g.json, h.columns ?? undefined, file)
        const doc = groupDoc(raw, h.columns ?? Object.keys(raw[0] ?? {}))
        const all = new IvGroup(h, doc, doc ? undefined : raw.map(toRow(h)))
        take(g.i, g.rg, all, true)
        settle(ivGroupKey(h, g.rg), all)
        if (h.dataKey && doc) putColoGroup(h, g.rg, doc)
      }
    })
  } finally {
    // A group this read abandoned (`stop`) or failed on: its waiters read nothing from it.
    for (const fk of [...mine.keys()]) settle(fk, null)
  }
  await Promise.all(waits)
  return out
}

/** Row groups in flight at once per read. Each group is its own range
 * fetch + decode; the fetch is ~200–240 ms of latency for ~280 KB, so the
 * reads are latency-bound, not bandwidth-bound: a root subtree's 24 groups
 * took three rounds at 8-wide (`Server-Timing` 2026-09-15: fetch 5.0 s
 * summed, 1.0 s wall) and a 7-day diff's 107 groups fourteen. 32 in flight
 * is ~9 MB of buffers at most — well inside the isolate's memory. */
export const GROUP_READS = 32
/** Concurrent span queries (D1, or footer-group decodes) one `readAsks` runs. */
const SPAN_QUERIES = 6

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

/** `selectSpans` refused: the rectangles' candidate groups exceed its cap. */
export class TooWide extends Error {}

/** Candidate row groups for a set of (depth, path-range) rectangles — one SQL
 * pass (no rg_json yet), carrying each group's stats so the caller can apply a
 * finer per-ask test. A group spanning a depth boundary resets path order, so
 * the path test only applies within a single depth (`d_min = d_max`). */
async function selectSpans(h: IndexHandle, rects: Rect[], cap = 4000, bMin = 0, lens?: Lens): Promise<Span[]> {
  return overTiers(h, t => selectSpans1(t, rects, cap, bMin, lens))
}

/** `select` over each of a handle's tiers (itself, then its runs), each span tagged with its tier. A run's failure
 *  cuts it out (`IvTierBroken`); a refusal (`TooWide`) is the read's. */
async function overTiers(h: IndexHandle, select: (t: IndexHandle) => Promise<Span[]>): Promise<Span[]> {
  if (!h.runs?.length) return select(h)
  const got = await Promise.all(tiersOf(h).map(async (t, i) => {
    try {
      const sp = await select(t)
      return i ? sp.map(x => ({ ...x, tier: i })) : sp
    } catch (e) {
      throw i && !(e instanceof TooWide) ? ivMarkBroken(h.env, t.run!, e) : e
    }
  }))
  return got.flat()
}

/** A read of tier `i` of `h`, a run's failure cutting it out (`IvTierBroken`). */
async function onTier<T>(h: IndexHandle, i: number, f: (t: IndexHandle) => Promise<T>): Promise<T> {
  const t = tiersOf(h)[i]
  try {
    return await f(t)
  } catch (e) {
    throw i ? ivMarkBroken(h.env, t.run!, e) : e
  }
}

async function selectSpans1(h: IndexHandle, rects: Rect[], cap = 4000, bMin = 0, lens?: Lens): Promise<Span[]> {
  if (h.mode !== 'd1') {
    const pass = (g: Parameters<typeof groupMatches>[0]) => groupMatches(g, rects, bMin, lens, lensSorted(h.variant))
    const out = h.mode === 'blob' ? h.groups.filter(pass) : await pqGroups(h, pass)
    if (out.length > cap) throw new TooWide(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
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
  if (res.results.length > cap) throw new TooWide(`query too wide: >${cap} row groups (drill deeper or raise minArea)`)
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
    for (const m of await mapLimit(fgs, GROUP_READS, fg => readFooterJson(h, fg))) for (const [rg, j] of m) if (want.has(rg)) out.set(rg, j)
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
 * holds it. What a lens's scattered assigned regions need. */
export async function readRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt?: (depth: number) => number,
  lens?: Lens,
  plan?: Span[],
  stop?: () => boolean,
): Promise<Row[]> {
  if (!rects.length) return []
  const kept = plan ?? await planRects(h, rects, thrAt, lens)
  // A row passes the lens iff its usr equals the key.
  const lensOk = (r: Row) => !lens || r.usr === lens.key
  const at = (depth: number, path: string) => rects.some(q => depth >= q.dLo && depth <= q.dHi && path >= q.pLo && path <= q.pHi)
  return decodeSpans(h, kept, r => at(r.depth, r.path) && lensOk(r), stop, at)
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
    for (const s of spans) byRg.set(spanKey(s), s)
  }
  h.trace?.('spans', now() - t0)
  return [...byRg.values()].sort(bySpan).filter(s => !thrAt || s.bMax >= thrAt(Math.max(s.dMin, dMin)))
}

/** Decode a planned set of groups (cached per isolate, `GROUP_READS` in
 * flight) and keep the rows `keep` accepts — the tail shared by both sorts'
 * subtree reads. Caps the group count and the rows decoded: a broad lens (a
 * big user spread across the estate) can select few-enough groups but still
 * decode millions of rows and blow the Worker CPU. Error cleanly instead. */
/** `pre`: a cheap superset of `keep` on a row's raw `depth`, `path` and `size` (`Pre`): an interval store's groups
 *  shape only the rows it passes. */
async function decodeSpans(h: IndexHandle, kept: Span[], keep: (r: Row) => boolean, stop?: () => boolean, pre?: Pre): Promise<Row[]> {
  if (kept.length > 250) throw new Error('query too wide: drill deeper or raise minArea')
  const totalRows = kept.reduce((n, s) => n + (s.rowEnd - s.rowStart), 0)
  if (totalRows > 700_000) throw new Error('query too wide: drill deeper or raise minArea')
  let t0 = now()
  if (h.runs?.length) {
    // Every tier's groups, every version of them, combined and then tested live.
    h.trace?.('ngroups', kept.length)
    const got = await Promise.all(tiersOf(h).map((_, i) => onTier(h, i, async t => {
      const mine = kept.filter(s => (s.tier ?? 0) === i)
      if (!mine.length) return []
      const jsons = await fetchGroupJson(t, mine.map(s => s.rg))
      return (await readGroupsCached(t, mine.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep), stop, true, pre)).flat()
    })))
    h.trace?.('groups', now() - t0, h.variant)
    return combineLive(h, got.flat())
  }
  const jsons = await fetchGroupJson(h, kept.map(s => s.rg))
  h.trace?.('rgjson', now() - t0)
  h.trace?.('ngroups', kept.length)
  t0 = now()
  const perGroup = await readGroupsCached(h, kept.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep), stop, false, pre)
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
    const hit = cachedGroup(h, rg)
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
        const D = h.asOf
        for (const i of hits) {
          const rec: Record<string, unknown> = { path: paths[i] }
          for (const [c, arr] of cols) rec[c] = arr[i]
          // An interval store's group holds every scan's versions: only those live at the handle's scan.
          if (D != null && !(num(rec.vf) <= D && D < num(rec.vt))) continue
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

/** An interval store's sorts ordered `(⌊log2 b⌋ desc, path, …)` with `b` the group bounds' size key: `size`, or
 *  the path's total `tot` (`slices-bytotal`). The user-first sorts lead with `usr` and are not among them. */
const SIZE_FIRST = new Set(['bysize', 'slices-bytotal'])

/** `⌊log2 b⌋` exactly (the writer's `length(bin(b)) - 1`), -1 for `b ≤ 0` (the last, NULL, bucket). */
export function sizeBucket(b: number): number {
  if (!(b > 0)) return -1
  let k = Math.floor(Math.log2(b))
  while (k > 0 && 2 ** k > b) k--
  while (2 ** (k + 1) <= b) k++
  return k
}

/** Whether a group of a size-first sort (`SIZE_FIRST`) can hold a row of `ranges` at or over `thrMin`, from its
 *  bounds and its neighbours' in the same segment — tighter than `groupMatchesSize` for a group spanning size
 *  buckets, whose min/max path is any bucket's (a group straddling a bucket boundary has the whole bucket
 *  keyspace as its path range, so a drilled prefix selects it though its rows of either bucket lie elsewhere).
 *  Rows are sorted by bucket descending, then path; so in a group whose buckets run from `T` (its `b_max`'s)
 *  down to `B` (its `b_min`'s):
 *  - its rows of bucket `T` follow the previous group's rows, and when that group is all of bucket `T` their
 *    paths are at least its `p_max` (its last row's path);
 *  - its rows of bucket `B` precede the next group's, at most its `p_min` when that group is all of bucket `B`;
 *  - a bucket whose largest size (`2^(b+1) - 1`) is under `thrMin` holds no row the read keeps.
 *  Groups without `b_min` / `seg` (an older footer) are kept. */
type SizeGroup = Pick<BlobGroup, 'pMin' | 'pMax' | 'bMax' | 'bMin' | 'seg'>
export function sizeNeighbours(g: SizeGroup, prev: SizeGroup | undefined, next: SizeGroup | undefined, ranges: { pLo: string; pHi: string }[], thrMin: number): boolean {
  if (g.bMin == null || g.seg == null) return true
  const T = sizeBucket(g.bMax)
  const B = sizeBucket(g.bMin)
  if (T === B) return true
  const whole = (n: SizeGroup | undefined, b: number) => n != null && n.seg === g.seg && n.bMin != null && sizeBucket(n.bMax) === b && sizeBucket(n.bMin) === b
  for (let b = B; b <= T; b++) {
    if (thrMin > 0 && (b < 0 || 2 ** (b + 1) - 1 < thrMin)) continue
    const lo = b === T && whole(prev, T) && prev!.pMax > g.pMin ? prev!.pMax : g.pMin
    const hi = b === B && whole(next, B) && next!.pMin < g.pMax ? next!.pMin : g.pMax
    if (ranges.some(r => hi >= r.pLo && lo < r.pHi)) return true
  }
  return false
}

/** Candidate row groups of the `bysize` sort for a set of path ranges at
 * one byte floor — one SQL pass per batch of ranges, no depth rect (the
 * depth test is per row, §1.3 "attenuation"). */
async function selectSizeSpans(h: IndexHandle, ranges: { pLo: string; pHi: string }[], thrMin: number, cap = 4000, lens?: Lens): Promise<Span[]> {
  return overTiers(h, t => selectSizeSpans1(t, ranges, thrMin, cap, lens))
}

async function selectSizeSpans1(h: IndexHandle, ranges: { pLo: string; pHi: string }[], thrMin: number, cap = 4000, lens?: Lens): Promise<Span[]> {
  if (h.mode !== 'd1') {
    const pass = (g: Parameters<typeof groupMatchesSize>[0]) => groupMatchesSize(g, ranges, thrMin, lens)
    // An interval store's size sort, unscoped: its groups' neighbours bound the size buckets a group spans.
    const near = h.mode === 'pq' && h.asOf != null && !lens && SIZE_FIRST.has(h.variant)
      ? (g: BlobGroup, prev?: BlobGroup, next?: BlobGroup) => sizeNeighbours(g, prev, next, ranges, thrMin)
      : undefined
    const out = h.mode === 'blob' ? h.groups.filter(pass) : await pqGroups(h, pass, near)
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
  // A run may hold paths deeper than its base's.
  if (h.runs?.length) return Math.max(...await Promise.all(tiersOf(h).map((t, i) => onTier(h, i, () => tierMaxDepth({ ...t, runs: undefined })))))
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
    for (const s of await selectSizeSpans(h, rects.slice(i, i + RANGES_PER_QUERY), thrMin, 4000, lens)) byRg.set(spanKey(s), s)
  }
  h.trace?.('spans', now() - t0)
  return [...byRg.values()].sort(bySpan)
}

/** Whether a size sort is keyed on each path's total (`tot`, spec
 * `bysize-path-total.md`): a labeled store's `bysize`, one row per owner slice,
 * sorted and grouped by `⌊log2 tot⌋` with `b_max = MAX(tot)`. */
export const keyedOnTotal = (h: IndexHandle): boolean => h.schema.some(l => l.name === 'tot')

/** A thresholded subtree read from the `bysize` sort (§2.1): the same
 * rows `readRects` returns from `path` at the same rects and `thrAt`, but
 * decoded from the groups above the threshold instead of the groups under
 * the path — `(rows under P with size ≥ thr) / group + #buckets` groups, so a
 * flat directory's children or a fleet root's view cost what they draw. The
 * per-row test is exact: depth in a rect, path in its range, `size ≥
 * thrAt(depth)`.
 *
 * Over owner slices the threshold is the path's, not a slice's: a sort keyed
 * on the path's total (`keyedOnTotal`) keeps every slice of each path whose
 * total clears `thrAt(depth)`. Every group holding a slice of such a path has
 * `b_max ≥ tot ≥` the read's floor and a path range meeting the rect, so all
 * of the path's slices are decoded and their sum is its `tot`; a path under
 * the threshold sums (in whole or in part) below it. A sort cut per slice
 * (before the re-cut) thresholds per slice: drawn dirs short of their small
 * slices, all-small-slice dirs missing. A lens reads one owner's bytes, so
 * its test stays per slice (sound on either cut: `MAX(tot) ≥ MAX(size)`). */
export async function readSizeRects(
  h: IndexHandle,
  rects: Rect[],
  thrAt: (depth: number) => number,
  lens?: Lens,
  plan?: Span[],
  stop?: () => boolean,
): Promise<Row[]> {
  if (!rects.length) return []
  const kept = plan ?? await planSizeRects(h, rects, thrAt, lens)
  const lensOk = (r: Row) => !lens || r.usr === lens.key
  const at = (depth: number, path: string) => rects.some(q => depth >= q.dLo && depth <= q.dHi && path >= q.pLo && path < q.pHi)
  if (lens || !keyedOnTotal(h)) return decodeSpans(h, kept, r => r.size >= thrAt(r.depth) && at(r.depth, r.path) && lensOk(r), stop, (d, p, sz) => sz >= thrAt(d) && at(d, p))
  const rows = await decodeSpans(h, kept, r => at(r.depth, r.path), stop, at)
  const tot = new Map<string, number>()
  const key = (r: Row) => `${r.depth}\0${r.path}`
  for (const r of rows) tot.set(key(r), (tot.get(key(r)) ?? 0) + r.size)
  return rows.filter(r => tot.get(key(r))! >= thrAt(r.depth))
}

/** One page of the size sort's candidate groups for a path range, in sort
 * (`rg`) order, after group `after` — `selectSizeSpans`'s predicate with no
 * byte floor, paged instead of capped. */
async function pageSizeSpans(h: IndexHandle, range: { pLo: string; pHi: string }, after: number, limit: number): Promise<Span[]> {
  if (h.mode !== 'd1') {
    const pass = (g: Parameters<typeof groupMatchesSize>[0]) => groupMatchesSize(g, [range])
    const all = h.mode === 'blob' ? h.groups.filter(pass) : await pqGroups(h, pass)
    return all.filter(g => g.rg > after).sort((a, b) => a.rg - b.rg).slice(0, limit)
  }
  const sql = 'SELECT rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? AND p_max >= ? AND p_min < ? AND rg > ? ORDER BY rg LIMIT ?'
  const res = await h.env.DB!.prepare(sql).bind(h.date, d1Variant(h.env, h.variant), h.gen, range.pLo, range.pHi, after, limit).all<{ rg: number; d_min: number; d_max: number; p_min: string; p_max: string; b_max: number; row_start: number; row_end: number }>()
  return res.results.map(r => ({ rg: r.rg, dMin: num(r.d_min), dMax: num(r.d_max), pMin: r.p_min, pMax: r.p_max, bMax: num(r.b_max), rowStart: num(r.row_start), rowEnd: num(r.row_end) }))
}

/** `⌊log2 b⌋`, the size sort's bucket (size 0: −1, below every bucket). */
const bucketOf = (b: number): number => (b > 0 ? Math.floor(Math.log2(b)) : -1)

/** Candidate groups fetched per span query, and decoded per round at most
 * (rounds start at one group and double: most answers need one or two). */
const LARGEST_PAGE = 32
const LARGEST_BATCH = 8
/** Groups a largest-files read decodes at most (a subtree whose top buckets hold mostly other paths' rows). */
export const LARGEST_MAX_GROUPS = 64

/** What `readLargest` answers: the files, size-descending (ties by path),
 * the size-sort groups it decoded, and whether files beyond them exist or
 * may (more matched than `n`, or candidate groups were left unread). */
export interface Largest { rows: Row[]; groups: number[]; decoded: number; more: boolean; capped: boolean }

/** The `n` largest objects (never dirs) under `path` (`''` = the store root),
 * from a store generation's `bysize` sort: its groups in sort order — largest
 * size bucket first — restricted by the span query to those whose path range
 * meets `[P/, P0)`, decoding each round's groups and keeping object rows
 * under P. A group whose `b_max` is below the current n-th size is skipped
 * undecoded, and the read stops once a group's top bucket is below the n-th
 * size's: buckets only fall along the sort, so no later group can hold a row
 * that big (within a bucket rows are path-sorted, so a same-bucket group may
 * still beat the n-th, and is read). At most `LARGEST_MAX_GROUPS` groups. */
export async function readLargest(h: IndexHandle, path: string, n: number, maxGroups = LARGEST_MAX_GROUPS): Promise<Largest> {
  const range = path === '' ? { pLo: '', pHi: '￿' } : { pLo: path + '/', pHi: path + '0' }
  const under = (p: string) => p >= range.pLo && p < range.pHi
  const order = (a: Row, b: Row) => b.size - a.size || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
  let top: Row[] = []
  let matched = 0
  const groups: number[] = []
  let decoded = 0
  let after = -1
  let pending: Span[] = []
  let exhausted = false
  let width = 1
  const nth = () => (top.length >= n ? top[n - 1].size : -1)
  for (;;) {
    if (!pending.length && !exhausted) {
      const t0 = now()
      pending = await pageSizeSpans(h, range, after, LARGEST_PAGE)
      h.trace?.('spans', now() - t0)
      if (pending.length < LARGEST_PAGE) exhausted = true
      if (pending.length) after = pending[pending.length - 1].rg
    }
    if (!pending.length) return { rows: top, groups, decoded, more: matched > n, capped: false }
    // This round: the next groups that could still place a row, up to the batch.
    const batch: Span[] = []
    const room = Math.min(width, maxGroups - groups.length)
    while (pending.length && batch.length < Math.max(1, room)) {
      const g = pending[0]
      const t = nth()
      // A group whose top bucket is below the n-th size's: it and every later group are out.
      if (t >= 0 && bucketOf(g.bMax) < bucketOf(t)) return { rows: top, groups, decoded, more: true, capped: false }
      pending.shift()
      if (t >= 0 && g.bMax < t) continue
      batch.push(g)
    }
    if (!batch.length) continue
    // Out of budget with a group still able to place a row.
    if (room <= 0) return { rows: top, groups, decoded, more: true, capped: true }
    const rows = await decodeSpans(h, batch, r => r.kind === 'file' && under(r.path))
    groups.push(...batch.map(g => g.rg))
    decoded += batch.reduce((s, g) => s + (g.rowEnd - g.rowStart), 0)
    matched += rows.length
    width = Math.min(LARGEST_BATCH, 2 * width)
    top = [...top, ...rows].sort(order).slice(0, n)
  }
}

/** Lookups this isolate found too wide (`readAsks`' `rememberWide`), oldest dropped past `TOO_WIDE_HELD`. */
const tooWide = new Map<string, string>()
const TOO_WIDE_HELD = 256

/** A lookup: one path, or every path under a prefix, at one depth. On an interval store an ask may
 *  bound the version it wants (`vtLe`: closed by then; `vfGe`: opened then or later) — what a diff knows
 *  from the other scan's version of the path, since a path's versions never overlap. */
export type Ask = ({ depth: number; path: string } | { depth: number; under: string }) & { vtLe?: number; vfGe?: number }

const askRect = (a: Ask): Rect =>
  'path' in a
    ? { dLo: a.depth, dHi: a.depth, pLo: a.path, pHi: a.path }
    : { dLo: a.depth, dHi: a.depth, pLo: a.under + '/', pHi: a.under + '0' } // '0' sorts just past '/'

const groupMayHold = (g: Span & { vfMax?: number; vtMin?: number }, a: Ask): boolean => {
  if (g.dMax < a.depth || g.dMin > a.depth) return false
  if (a.vtLe != null && g.vtMin != null && g.vtMin > a.vtLe) return false
  if (a.vfGe != null && g.vfMax != null && g.vfMax < a.vfGe) return false
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
  { columns, maxGroups = 60, spanCap = 4000, stop, rememberWide }: { columns?: string[]; maxGroups?: number; spanCap?: number; stop?: () => boolean; rememberWide?: boolean } = {},
): Promise<{ rows: Row[]; groups: number }> {
  // `rememberWide`: a lookup this isolate already found too wide (same generation, asks and budget) throws
  // before any span query — the answer can't change while the generation stands.
  const wideKey = rememberWide ? `${h.mode}\0${h.date}\0${h.variant}\0${'gen' in h ? h.gen : ''}\0${maxGroups}\0${asks.map(a => `${a.depth}:${'path' in a ? a.path : `${a.under}/`}`).join('\n')}` : null
  const known = wideKey != null ? tooWide.get(wideKey) : undefined
  if (known) throw new Error(`lookup too wide: ${known} (cap ${maxGroups}, remembered)`)
  // One rectangle per depth over its asks' [min, max] path, halved while it
  // selects more groups than its asks could need: sparse asks across a deep
  // store (gcs's ~1,260 assignment prefixes over 8k-row groups) would otherwise
  // select every group between the first ask and the last, past any cap.
  // Exact: each ask stays in exactly one rectangle, a single ask over the cap
  // still throws, and `groupMayHold` + `keep` narrow what is read.
  const lo = (a: Ask) => askRect(a).pLo
  const hi = (a: Ask) => askRect(a).pHi
  const byDepth = new Map<number, Ask[]>()
  for (const a of asks) {
    const l = byDepth.get(a.depth)
    if (l) l.push(a)
    else byDepth.set(a.depth, [a])
  }
  let parts = [...byDepth.entries()].map(([depth, l]) => ({ depth, asks: l.sort((x, y) => (lo(x) < lo(y) ? -1 : lo(x) > lo(y) ? 1 : 0)) }))
  const found = new Map<number, Span>()
  // The group budget is checked as the plan grows, not after it: once the groups found pass `maxGroups` the
  // lookup can only fail, so no further span query (nor halving) is started — wide asks (a filter's 48 root
  // details over many depths) paid every span query and then threw anyway.
  const over = () => found.size > maxGroups
  let cut = false
  let t0 = now()
  while (parts.length && !over()) {
    const next: typeof parts = []
    await mapLimit(parts, SPAN_QUERIES, async ({ depth, asks: part }) => {
      if (over()) { cut = true; return }
      const rect = { dLo: depth, dHi: depth, pLo: lo(part[0]), pHi: part.reduce((m, a) => (hi(a) > m ? hi(a) : m), hi(part[0])) }
      const cap = part.length > 1 ? Math.min(spanCap, 4 * part.length + 16) : spanCap
      let cand: Span[]
      h.trace?.('askspan', 1)
      try {
        cand = await selectSpans(h, [rect], cap)
      } catch (e) {
        if (!(e instanceof TooWide) || part.length === 1) throw e
        const m = part.length >> 1
        next.push({ depth, asks: part.slice(0, m) }, { depth, asks: part.slice(m) })
        return
      }
      for (const s of cand) if (!found.has(spanKey(s)) && part.some(a => groupMayHold(s, a))) found.set(spanKey(s), s)
    })
    parts = next
  }
  if (parts.length) cut = true
  h.trace?.('spans', now() - t0)
  const spans = [...found.values()].sort(bySpan)
  if (over()) {
    const n = `${cut ? '≥' : ''}${spans.length} row groups`
    if (wideKey != null) {
      tooWide.set(wideKey, n)
      if (tooWide.size > TOO_WIDE_HELD) tooWide.delete(tooWide.keys().next().value!)
    }
    throw new Error(`lookup too wide: ${n} (cap ${maxGroups})`)
  }
  t0 = now()
  if (h.runs?.length) {
    h.trace?.('ngroups', spans.length)
    const got = await Promise.all(tiersOf(h).map((_, i) => onTier(h, i, async t => {
      const mine = spans.filter(s => (s.tier ?? 0) === i)
      if (!mine.length) return []
      const jsons = await fetchGroupJson(t, mine.map(s => s.rg))
      const per = columns
        ? await mapLimit(mine, GROUP_READS, async s => {
          const j = jsons.get(s.rg)
          return j ? (await readGroupVersions(t, j, columns)).filter(keep) : []
        })
        : await readGroupsCached(t, mine.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep), stop, true)
      return per.flat()
    })))
    h.trace?.('groups', now() - t0)
    return { rows: combineLive(h, got.flat()), groups: spans.length }
  }
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
    : await readGroupsCached(h, spans.flatMap(s => { const json = jsons.get(s.rg); return json ? [{ rg: s.rg, json }] : [] }), rows => rows.filter(keep), stop)
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
