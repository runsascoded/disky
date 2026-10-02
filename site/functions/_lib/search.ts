/**
 * The search index reader (specs/path-store-search.md §4): a filter query →
 * its match roots under a view root, from the generation's search sidecars
 * beside the `path` sort. Two layouts (§2):
 *
 * - v2: `path-index.rows-search.parquet` (the directory), `.trigrams.parquet`
 *   (trigram → name-id postings) and `.rows.parquet` (the store's rows
 *   name-major: a candidate name's rows are verified and read in one place);
 * - v1: `path-index.search.parquet`, `.trigrams.parquet` and
 *   `.names.parquet` (the vocabulary, each name's `path` row groups), then
 *   those `path` row groups.
 *
 * No D1 rows: every file is range-read (neighbouring groups merged) through
 * the colo cache (a generation dir is immutable), and decoded groups share
 * the isolate's LRU with the tier reads.
 *
 * Exactness: the planner (`searchQuery.ts`) yields a superset of the names a
 * match root's last segment can have; every row read is tested with the
 * view's own predicate; each matching row is lifted to its shallowest
 * matching prefix. Unless a budget cut the search (`truncated`), every
 * root's rows were among those read — the roots equal `matchRoots` over
 * every row under the view root.
 */
import { type FileMetaData, parquetMetadata, parquetReadObjects, type RowGroup } from 'hyparquet'
import type { Env } from './auth.js'
import { bufferSlice, cacheGet, cachePut, cachedRange, chunkSpan, decodeColumns, type IndexHandle, mapLimit, num, planRuns, readAsks, readFooterBytes, readGroupsAt, reviveRowGroup, type Row, RUN_READS, runsInOrder, str, toRow } from './index.js'
import type { NamePred } from './scope.js'
import { type Formula, or, type SearchPlan } from './searchQuery.js'
import { storeKey } from './stores.js'
import { compressors } from './zstd.js'

/** The sidecars' names beside the `path` sort (`disk_tree.find.search`):
 * v2's own directory first, else v1's. */
export const SEARCH_FILES = {
  rows: 'path-index.rows.parquet',
  rowsSearch: 'path-index.rows-search.parquet',
  names: 'path-index.names.parquet',
  trigrams: 'path-index.trigrams.parquet',
  search: 'path-index.search.parquet',
} as const
export const searchKey = (dir: string, role: keyof typeof SEARCH_FILES): string => `${dir}/${SEARCH_FILES[role]}`

/** Per-request budgets (spec §4.2–4.3). Hitting `namesRgs`, `pathRgs`,
 * `rowsRgs`, `keptRows`, `liftGroups` or `wallMs` stops the search with a
 * `reason` the response carries as `partial` — never a silent cut. */
export interface SearchLimits {
  /** Postings row groups a trigram may span and still be read; wider is
   * unselective and treated as no constraint (sound: only widens). */
  triRgs: number
  /** v1: names row groups read to verify candidates (heaviest ids first). */
  namesRgs: number
  /** v1: `path`-sort row groups read for the verified names' rows. */
  pathRgs: number
  /** v2: rows-file row groups read (heaviest ids first) — the decode budget. */
  rowsRgs: number
  /** Matching rows kept: past it no further group is decoded — the bound
   * on the request's memory and on the roots it returns. */
  keptRows: number
  /** Row groups the lifted-root point lookup may touch (truncated only). */
  liftGroups: number
  /** Wall-clock budget, ms: checked before each stage and each merged range
   * read; past it the search stops where it is (`partial`) — the guard for
   * a store or query no read budget anticipated. */
  wallMs: number
}
export const SEARCH_LIMITS: SearchLimits = { triRgs: 32, namesRgs: 128, pathRgs: 96, rowsRgs: 256, keptRows: 50_000, liftGroups: 120, wallMs: 1500 }

interface SchemaElement { type: string; name: string; repetition_type: string; converted_type?: string }

/** One directory row: a row group of the names (`file` 0) or trigrams (1) file. */
interface DirRow { file: number; rg: number; rowStart: number; rowEnd: number; kMin: number; kMax: number; rgJson: string }

/** One row group of the directory itself, with the bounds its stats give. */
interface DirGroup {
  n: number
  byteStart: number
  byteEnd: number
  meta: RowGroup
  bounds: { fileMin: number; fileMax: number; rgMin: number; rgMax: number; kLo: number; kHi: number } | null
}

interface SearchIndex {
  /** The layout (§2): 2 = name-major rows, 1 = names + `path` groups. */
  v: 1 | 2
  key: string
  dir: string
  metadata: FileMetaData
  groups: DirGroup[]
  /** `file` 0's schema: the rows (v2) or the names (v1). */
  data: SchemaElement[]
  trigrams: SchemaElement[]
}

const DIR_COLS = ['file', 'rg', 'row_start', 'row_end', 'k_min', 'k_max', 'rg_json']
const MISS_TTL = 60_000
const opened = new Map<string, { at: number; idx: Promise<SearchIndex | null> }>()

/** A generation's search directory — v2's, else v1's — or null when it
 * has neither (a generation written without `-S`, every v1 index).
 * Remembered per isolate (a miss for `MISS_TTL`). */
export function openSearch(env: Env, dir: string): Promise<SearchIndex | null> {
  const ck = `${storeKey(env)}|${dir}|search`
  const hit = opened.get(ck)
  if (hit && Date.now() - hit.at < MISS_TTL) return hit.idx
  const idx = (async (): Promise<SearchIndex | null> => {
    for (const [v, role] of [[2, 'rowsSearch'], [1, 'search']] as const) {
      const key = searchKey(dir, role)
      let buf: ArrayBuffer
      try {
        buf = await readFooterBytes(env, key)
      } catch (e) {
        if ((e as Error).name === 'NotFoundError') continue
        throw e
      }
      const metadata = parquetMetadata(buf)
      const kv = new Map((metadata.key_value_metadata ?? []).map(e => [e.key, e.value]))
      if (kv.get('search_v') !== String(v)) throw new Error(`${key}: not a v${v} search directory (search_v=${kv.get('search_v')})`)
      const groups = metadata.row_groups.map((meta, n): DirGroup => {
        const cols = new Map(meta.columns.map(c => [c.meta_data!.path_in_schema[0], c.meta_data!]))
        const [byteStart, byteEnd] = chunkSpan(meta as unknown as Parameters<typeof chunkSpan>[0])
        const st = (col: string, side: 'min' | 'max') => { const s = cols.get(col)?.statistics; return side === 'min' ? (s?.min_value ?? s?.min) : (s?.max_value ?? s?.max) }
        const b = [st('file', 'min'), st('file', 'max'), st('rg', 'min'), st('rg', 'max'), st('k_min', 'min'), st('k_max', 'max')]
        const bounds = b.some(v => v == null) ? null : { fileMin: num(b[0]), fileMax: num(b[1]), rgMin: num(b[2]), rgMax: num(b[3]), kLo: num(b[4]), kHi: num(b[5]) }
        return { n, byteStart, byteEnd, meta, bounds }
      })
      return {
        v, key, dir, metadata, groups,
        data: JSON.parse(kv.get(v === 2 ? 'rows_schema' : 'names_schema') ?? '[]') as SchemaElement[],
        trigrams: JSON.parse(kv.get('trigrams_schema') ?? '[]') as SchemaElement[],
      }
    }
    return null
  })()
  opened.set(ck, { at: Date.now(), idx })
  idx.catch(() => opened.delete(ck))
  return idx
}

/** Directory groups, each decoded once per isolate: the misses as merged
 * range reads (colo-cached), results in input order. */
async function readDirGroups(env: Env, idx: SearchIndex, gs: DirGroup[]): Promise<DirRow[][]> {
  const out: DirRow[][] = new Array(gs.length)
  const miss: { i: number; g: DirGroup; k: string; start: number; end: number }[] = []
  gs.forEach((g, i) => {
    const k = `${storeKey(env)}|${idx.key}|d${g.n}`
    const hit = cacheGet<DirRow>(k)
    if (hit) out[i] = hit
    else miss.push({ i, g, k, start: g.byteStart, end: g.byteEnd })
  })
  await mapLimit(planRuns(miss), RUN_READS, async run => {
    const file = bufferSlice(await cachedRange(env, idx.key, run.start, run.end), run.start)
    for (const m of run.items) {
      const metadata = { ...idx.metadata, row_groups: [m.g.meta], num_rows: m.g.meta.num_rows }
      const rows = (await parquetReadObjects({ file, metadata, columns: DIR_COLS, compressors })) as Record<string, unknown>[]
      const dr = rows.map((r): DirRow => ({ file: num(r.file), rg: num(r.rg), rowStart: num(r.row_start), rowEnd: num(r.row_end), kMin: num(r.k_min), kMax: num(r.k_max), rgJson: str(r.rg_json) }))
      cachePut(m.k, dr, dr.reduce((n, r) => n + 64 + 2 * r.rgJson.length, 0))
      out[m.i] = dr
    }
  })
  return out
}

/** A directory this small is read whole on first use (one round trip
 * instead of one per lookup: the trigrams', then the candidates'). */
const DIR_WHOLE = 1 << 20

/** Directory rows `row` accepts, from the groups whose bounds `group` admits
 * (a group without stats always), in directory order; `stop` ends the scan
 * once enough rows are in (groups are read 32 at a time). */
async function dirRows(
  env: Env,
  idx: SearchIndex,
  group: (b: NonNullable<DirGroup['bounds']>) => boolean,
  row: (r: DirRow) => boolean,
  stop: (got: DirRow[]) => boolean = () => false,
): Promise<{ rows: DirRow[]; groups: number }> {
  const last = idx.groups[idx.groups.length - 1]
  if (last && last.byteEnd - idx.groups[0].byteStart <= DIR_WHOLE) await readDirGroups(env, idx, idx.groups)
  const sel = idx.groups.filter(g => !g.bounds || group(g.bounds))
  const out: DirRow[] = []
  let read = 0
  for (let i = 0; i < sel.length && !stop(out); i += 32) {
    const batch = sel.slice(i, i + 32)
    read += batch.length
    for (const rs of await readDirGroups(env, idx, batch)) for (const r of rs) if (row(r)) out.push(r)
  }
  return { rows: out, groups: read }
}

/** Many data row groups (names or trigrams) from their directory rows,
 * each through the decoded-group LRU under `tag`: the misses' byte spans
 * (their revived `RowGroup`s) merged into range reads (`planRuns` —
 * neighbouring ids / trigrams are adjacent in the file), each colo-cached,
 * `RUN_READS` in flight, every group decoded from its run's bytes and
 * shaped by `shape` (results in input order). */
async function readDataGroups<T>(
  env: Env,
  idx: SearchIndex,
  role: 'names' | 'trigrams',
  ds: DirRow[],
  columns: string[],
  tag: string,
  shape: (rows: Record<string, unknown>[]) => { v: T; bytes: number },
): Promise<T[]> {
  const schema = role === 'names' ? idx.data : idx.trigrams
  const key = searchKey(idx.dir, role)
  const out: T[] = new Array(ds.length)
  const miss: { i: number; k: string; rg: RowGroup; start: number; end: number }[] = []
  ds.forEach((d, i) => {
    const k = `${storeKey(env)}|${idx.key}|${tag}${d.rg}`
    const hit = cacheGet<T>(k)
    if (hit) { out[i] = hit[0]; return }
    const rg = reviveRowGroup(d.rgJson, schema) as unknown as RowGroup
    const [start, end] = chunkSpan(rg as unknown as Parameters<typeof chunkSpan>[0])
    miss.push({ i, k, rg, start, end })
  })
  await mapLimit(planRuns(miss), RUN_READS, async run => {
    const file = bufferSlice(await cachedRange(env, key, run.start, run.end), run.start)
    for (const m of run.items) {
      const metadata = { version: 1, schema, num_rows: m.rg.num_rows, row_groups: [m.rg], metadata_length: 0 } as unknown as FileMetaData
      const { v, bytes } = shape((await parquetReadObjects({ file, metadata, columns, compressors })) as Record<string, unknown>[])
      cachePut(m.k, [v], bytes)
      out[m.i] = v
    }
  })
  return out
}

/** v2: the rows of rows-file groups whose path `keep` accepts, each with
 * its name id: merged range reads (colo-cached) handed over in id order
 * (`runsInOrder`); per group the `path` column first, the other columns —
 * and rows — only for the kept paths. `stop(kept)` is asked before each
 * group; `read` names the groups decoded. */
async function readRows(env: Env, idx: SearchIndex, h: IndexHandle, ds: DirRow[], keep: (path: string) => boolean, stop: (kept: number) => boolean): Promise<{ rows: Row[]; ids: Map<Row, number>; read: Set<number> }> {
  const key = searchKey(idx.dir, 'rows')
  const have = new Set(idx.data.slice(1).map(e => e.name))
  const rest = ['id', ...(h.columns ?? []).filter(c => c !== 'path' && have.has(c))]
  const shape = toRow(h)
  const ids = new Map<Row, number>()
  const read = new Set<number>()
  const out: Row[] = []
  const spans = ds.map(d => {
    const rg = reviveRowGroup(d.rgJson, idx.data) as unknown as RowGroup
    const [start, end] = chunkSpan(rg as unknown as Parameters<typeof chunkSpan>[0])
    return { d, rg, start, end }
  })
  let halted = false
  const halt = () => (halted ||= stop(out.length))
  await runsInOrder(planRuns(spans), run => cachedRange(env, key, run.start, run.end), async (run, buf) => {
    const file = bufferSlice(buf, run.start)
    for (const g of run.items) {
      if (halt()) return
      const paths = (await decodeColumns(idx.data, g.rg, file, ['path'])).get('path')!
      const hits: number[] = []
      for (let i = 0; i < paths.length; i++) if (keep(str(paths[i]))) hits.push(i)
      if (hits.length) {
        const cols = await decodeColumns(idx.data, g.rg, file, rest)
        const id = cols.get('id')!
        for (const i of hits) {
          const rec: Record<string, unknown> = { path: paths[i] }
          for (const [c, arr] of cols) rec[c] = arr[i]
          const row = shape(rec)
          ids.set(row, num(id[i]))
          out.push(row)
        }
      }
      read.add(g.d.rg)
    }
  }, halt)
  return { rows: out, ids, read }
}

/** Postings groups as parallel arrays. */
const readPostings = (env: Env, idx: SearchIndex, ds: DirRow[]) =>
  readDataGroups(env, idx, 'trigrams', ds, ['tri', 'id'], 't', rows => ({
    v: { tri: Int32Array.from(rows, r => num(r.tri)), id: Int32Array.from(rows, r => num(r.id)) },
    bytes: 8 * rows.length + 64,
  }))

interface NameRow { id: number; name: string; nRgs: number; rgs: string | null }

/** Names groups. */
const readNames = (env: Env, idx: SearchIndex, ds: DirRow[]) =>
  readDataGroups(env, idx, 'names', ds, ['id', 'name', 'n_rgs', 'rgs'], 'n', rows => {
    const v = rows.map((r): NameRow => ({ id: num(r.id), name: str(r.name), nRgs: num(r.n_rgs), rgs: r.rgs == null ? null : str(r.rgs) }))
    return { v, bytes: v.reduce((n, r) => n + 48 + 2 * (r.name.length + (r.rgs?.length ?? 0)), 0) }
  })

/** The trigrams a formula mentions. */
function trisOf(f: Formula, out = new Set<number>()): Set<number> {
  if (f === true) return out
  if (typeof f === 'number') out.add(f)
  else for (const x of 'and' in f ? f.and : f.or) trisOf(x, out)
  return out
}

/** The formula with every trigram `keep` rejects relaxed to `true`. */
function relax(f: Formula, keep: (t: number) => boolean): Formula {
  if (f === true) return true
  if (typeof f === 'number') return keep(f) ? f : true
  const xs = ('and' in f ? f.and : f.or).map(x => relax(x, keep))
  if ('and' in f) {
    const ys = xs.filter(x => x !== true)
    return ys.length === 0 ? true : ys.length === 1 ? ys[0] : { and: ys }
  }
  return or(xs)
}

/** Ids satisfying a (non-`true`) formula, from each trigram's sorted ids. */
function evaluate(f: Formula, lists: Map<number, number[]>): Set<number> {
  if (f === true) throw new Error('evaluate: an unconstrained formula has no finite id set')
  if (typeof f === 'number') return new Set(lists.get(f) ?? [])
  const sets = ('and' in f ? f.and : f.or).map(x => evaluate(x, lists))
  if ('or' in f) {
    const u = new Set<number>()
    for (const s of sets) for (const x of s) u.add(x)
    return u
  }
  sets.sort((a, b) => a.size - b.size)
  let acc = sets[0]
  for (const s of sets.slice(1)) acc = new Set([...acc].filter(x => s.has(x)))
  return acc
}

/** The first index in sorted `xs` with `xs[i] >= v`. */
function lowerBound(xs: number[], v: number): number {
  let lo = 0
  let hi = xs.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (xs[mid] < v) lo = mid + 1
    else hi = mid
  }
  return lo
}
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
const anyIn = (xs: number[], lo: number, hi: number): boolean => { const i = lowerBound(xs, lo); return i < xs.length && xs[i] <= hi }

export interface SearchFound {
  /** The match roots' own rows (every owner slice), lifted ones included. */
  rows: Row[]
  /** The outermost matches strictly under the view root, sorted. */
  roots: string[]
  /** A budget cut the search: the roots are those of the heaviest names. */
  truncated: boolean
  /** Why, when `truncated` (what the response's `partialReason` says). */
  reason?: string
  stats: {
    /** The sidecars' layout (§2). */
    layout: 1 | 2
    /** `trigrams`: candidates from postings; `scan`: the names in id order. */
    mode: 'trigrams' | 'scan'
    trigrams: number
    postingsRgs: number
    candidates: number
    dirGroups: number
    /** v1: names groups read. */
    namesRgs: number
    /** Names with a row read: v1 the verified candidates, v2 the names of
     * the matching rows. */
    names: number
    /** v1: `path` groups read. */
    pathRgs: number
    /** v2: rows groups read. */
    rowsRgs: number
    /** Roots fetched by a point lookup (a cut search's). */
    lifted: number
  }
}

/** Decoded search results, per isolate: a filter's first paint (`depth=1`)
 * and its full view run the same search back to back, as do a view's
 * re-renders at other widths. Keyed by the generation dir, the root, the
 * caller's query key and the limits; a result the wall clock cut is never
 * kept (a warm retry may finish). */
const FOUND_BYTES = 160 // per row, as the group LRU estimates a `Row`

/** The outermost paths strictly under `root` that pass `query`, on a store
 * generation's `path` sort `h`, given `plan` — the planner's candidate names
 * for the last segment of every such path (`planPositive` for match roots,
 * `planNegative` for excluded paths). Null when the index can't answer (no
 * sidecar for the generation): the caller reads as before. `query` must be
 * an interval on every root-to-leaf chain (false, then true, then false —
 * `pos ∧ ¬neg`), so a matching path's outermost match is its shallowest
 * matching prefix. `key` (the query's identity, e.g. its AST) enables the
 * per-isolate result cache. */
export async function searchRoots(env: Env, h: IndexHandle, query: NamePred, plan: SearchPlan, root: string, limits: SearchLimits = SEARCH_LIMITS, key?: string): Promise<SearchFound | null> {
  const ck = key && `${storeKey(env)}|${h.dir}|found|${root}|${key}|${JSON.stringify(limits)}`
  const hit = ck ? cacheGet<SearchFound>(ck) : undefined
  if (hit) return hit[0]
  const t0 = Date.now()
  const late = () => Date.now() - t0 >= limits.wallMs
  const idx = await openSearch(env, h.dir)
  if (!idx) return null
  let dirGroups = 0

  // Candidates: the postings of the selective trigrams, per the formulas.
  const tris = [...new Set(plan.branches.flatMap(b => [...trisOf(b.formula)]))].sort((a, b) => a - b)
  const triDir = new Map<number, DirRow[]>()
  if (tris.length) {
    const got = await dirRows(env, idx,
      b => b.fileMin <= 1 && b.fileMax >= 1 && anyIn(tris, b.kLo, b.kHi),
      r => r.file === 1 && anyIn(tris, r.kMin, r.kMax))
    dirGroups += got.groups
    for (const t of tris) triDir.set(t, got.rows.filter(r => r.kMin <= t && t <= r.kMax))
  }
  const formula = or(plan.branches.map(b => relax(b.formula, t => triDir.get(t)!.length <= limits.triRgs)))
  let candidates: number[] | null = null // null = the names scan
  let postingsRgs = 0
  if (formula !== true) {
    const used = trisOf(formula)
    const groups = new Map<number, DirRow>()
    for (const t of used) for (const r of triDir.get(t)!) groups.set(r.rg, r)
    postingsRgs = groups.size
    const lists = new Map<number, number[]>([...used].map(t => [t, []]))
    for (const p of await readPostings(env, idx, [...groups.values()])) {
      for (let i = 0; i < p.tri.length; i++) lists.get(p.tri[i])?.push(p.id[i])
    }
    candidates = [...evaluate(formula, lists)].sort((a, b) => a - b)
  }

  let truncated = false
  let timedOut = false
  const reasons: string[] = []
  const cut = (why: string) => { truncated = true; if (!reasons.includes(why)) reasons.push(why) }
  const overTime = () => {
    if (!late()) return false
    timedOut = true
    cut(`the search hit its time budget (${(limits.wallMs / 1000).toFixed(1)} s)`)
    return true
  }
  // The rows kept bound the request's memory (and the response: a root per
  // row, at most).
  const halt = (kept: number) => {
    if (kept > limits.keptRows) { cut(`more than ${limits.keptRows.toLocaleString('en-US')} matching rows`); return true }
    return overTime()
  }
  const under = root === '' ? () => true : (p: string) => p.startsWith(root + '/')
  const keep = (p: string) => under(p) && query(p)

  // The candidates' `file` 0 groups (rows / names), heaviest ids first —
  // the names scan reads them from id 0. `cap` groups at most (one more is
  // selected, to tell a cut).
  const cap = idx.v === 2 ? limits.rowsRgs : limits.namesRgs
  let dataGroups: DirRow[] = []
  if (candidates === null) {
    const got = await dirRows(env, idx,
      b => b.fileMin <= 0 && b.fileMax >= 0 && b.rgMin <= cap,
      r => r.file === 0 && r.rg <= cap,
      rs => rs.length > cap)
    dirGroups += got.groups
    dataGroups = got.rows.sort((a, b) => a.rg - b.rg)
  } else if (candidates.length) {
    const cs = candidates
    const got = await dirRows(env, idx,
      b => b.fileMin <= 0 && b.fileMax >= 0 && anyIn(cs, b.kLo, b.kHi),
      r => r.file === 0 && anyIn(cs, r.kMin, r.kMax),
      rs => rs.length > cap)
    dirGroups += got.groups
    dataGroups = got.rows.sort((a, b) => a.rg - b.rg)
  }
  let unread: DirRow[] = []
  if (dataGroups.length > cap) {
    cut(idx.v === 2
      ? `the row read hit its budget (${plural(cap, 'row group')})`
      : `the name search hit its read budget (${plural(cap, 'name group')})`)
    unread = dataGroups.slice(cap)
    dataGroups = dataGroups.slice(0, cap)
  }
  if (dataGroups.length && overTime()) { unread = [...dataGroups, ...unread]; dataGroups = [] }

  let read: Row[]
  /** Whether a root's rows were all read, given one of them. */
  let whole: (r: Row) => boolean
  let names = 0
  let pathRead = 0
  let rowsRead = 0
  if (idx.v === 2) {
    // v2: the groups hold the candidates' rows themselves. A name is whole
    // unless an unread group may hold some of its rows: ids at or past the
    // first unread group's.
    const got = await readRows(env, idx, h, dataGroups, keep, halt)
    read = got.rows
    rowsRead = got.read.size
    for (const d of dataGroups) if (!got.read.has(d.rg)) unread.push(d)
    const cutoff = unread.reduce((m, d) => Math.min(m, d.kMin), Infinity)
    whole = r => got.ids.get(r)! < cutoff
    names = new Set(got.ids.values()).size
  } else {
    // v1: verify the candidates on their names, then read the verified
    // names' `path` groups, heaviest first, within the budget.
    const want = candidates && new Set(candidates)
    const verified: NameRow[] = []
    for (const rs of await readNames(env, idx, dataGroups)) {
      for (const r of rs) if ((!want || want.has(r.id)) && plan.branches.some(b => b.test(r.name))) verified.push(r)
    }
    verified.sort((a, b) => a.id - b.id)
    names = verified.length
    const pathRgs = new Set<number>()
    for (const n of verified) {
      if (n.rgs == null) { cut(`the name “${n.name}” is spread over too many row groups to read`); break }
      const add = n.rgs.split(',').map(Number).filter(rg => !pathRgs.has(rg))
      if (pathRgs.size + add.length > limits.pathRgs) { cut(`the row read hit its budget (${plural(limits.pathRgs, 'path group')})`); break }
      for (const rg of add) pathRgs.add(rg)
    }
    if (pathRgs.size && overTime()) pathRgs.clear()
    const got = await readGroupsAt(h, [...pathRgs].sort((a, b) => a - b), keep, halt)
    read = got.rows
    pathRead = got.read.size
    // A verified name is complete when every group holding its rows was
    // read (a lighter name sharing a heavier one's group is, too); a root is
    // known whole only through a complete name of its own — owner slices of
    // one path can straddle a group boundary.
    const complete = new Set(verified.filter(n => n.rgs != null && n.rgs.split(',').every(rg => got.read.has(Number(rg)))).map(n => n.name))
    whole = r => complete.has(r.path.slice(r.path.lastIndexOf('/') + 1))
  }

  // Roots: each matching row's shallowest matching prefix below the root.
  const dRoot = root === '' ? 0 : root.split('/').length
  const outermost = (p: string): string => {
    const segs = p.split('/')
    for (let k = dRoot + 1; k < segs.length; k++) {
      const q = segs.slice(0, k).join('/')
      if (query(q)) return q
    }
    return p
  }
  const roots = new Set<string>()
  for (const r of read) roots.add(outermost(r.path))
  const rows = read.filter(r => roots.has(r.path) && whole(r))
  const have = new Set(rows.map(r => r.path))
  const missing = [...roots].filter(p => !have.has(p))
  let lifted = 0
  if (missing.length) {
    // Only reachable when truncated (a root's name was cut, or its groups
    // not all read) — or if the DuckDB/JS lowercase tables ever disagree on
    // a name; either way the root is real and its rows are one point lookup
    // away.
    const miss = new Set(missing)
    if (!overTime()) {
      try {
        const lk = await readAsks(h, missing.map(p => ({ depth: p.split('/').length, path: p })), r => miss.has(r.path), { maxGroups: limits.liftGroups })
        rows.push(...lk.rows)
        for (const r of lk.rows) if (miss.delete(r.path)) lifted++
      } catch (e) {
        if (!String((e as Error).message).startsWith('lookup too wide')) throw e
      }
    }
    if (miss.size) {
      cut(`${plural(miss.size, 'match root')} too wide to look up`)
      for (const p of miss) roots.delete(p)
    }
  }
  const found: SearchFound = {
    rows,
    roots: [...roots].sort(),
    truncated,
    ...(truncated ? { reason: reasons.join('; ') } : {}),
    stats: { layout: idx.v, mode: candidates === null ? 'scan' : 'trigrams', trigrams: tris.length, postingsRgs, candidates: candidates?.length ?? 0, dirGroups, namesRgs: idx.v === 1 ? dataGroups.length : 0, names, pathRgs: pathRead, rowsRgs: rowsRead, lifted },
  }
  if (ck && !timedOut) cachePut(ck, [found], 256 + rows.length * FOUND_BYTES)
  return found
}
