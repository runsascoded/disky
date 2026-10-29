/** The heavy terms' drilldown (specs/architecture/static-name-search.md, "Drilldown for heavy terms"): the
 *  static catalog's members — the literals whose suffix range holds more than V rows, and every one- and
 *  two-character literal — answered at any view path P from their **match roots** (`static_roots.py`).
 *
 *  Layout, under `static-names/<gen>/drill/`: per kind (`long`, `short`) the roots files (`q, path, usr, vf,
 *  vt, size, n_files`, epoch seconds, sorted `(q, path, usr, vf)`) and the rollups (`q, dir, kind, child, vf,
 *  b, o`, sorted `(q, dir, kind, child, vf)`; kind 0 the header — `vf` children kept, `b` root rows under
 *  `dir`, `o` children holding roots — 1 a kept child's running totals, 2 the remainder's), each set with
 *  a two-level group index (`<kind>-<set>-index.parquet`, one entry per data row group: its exact first
 *  and last `(q, key)`, byte span, rows and column chunk offsets; and its `.top.parquet`, one entry per
 *  index row group); `aliases.parquet` maps every long member to the canonical member whose rows it shares
 *  (equal root sets); `meta.json` holds R, K and `dispatch_rows` (R + 2 row groups).
 *
 *  A view (`Drill.view`, exactly `static_roots.Drill.view`): P's lowercase path holding the literal → the
 *  plain view. Else the roots key range `[(c, P/), (c, P0))` (the whole member at the fleet root) is bounded
 *  from the indexes alone: ≤ `dispatch_rows` rows → read them (match roots, like the light path's first
 *  hits); more → `(c, P)` is heavy, and its rollup gives each child's totals on any date (K kept children
 *  plus an exact remainder). The fleet root has no rollup (heavy directories are depth ≥ 1): there the
 *  catalog's per-bucket cells are the same thing, every bucket kept.
 *
 *  Runs (specs/static-daily-append.md, "Drilldown runs (heavy terms)"): each scan past the base adds a tier,
 *  `<run>/drill/` in the base's layout (2,048-row groups), live iff its `meta.json` exists. A run holds its
 *  scan's first-hit opens and close records per class canonical, its own alias map (classes only split), and
 *  for each heavy directory it touched either a delta header (`kind` −1) and the cells dated at its scan, or a
 *  full restatement (`kind` 0). The reader (`Drill`, as the Python `static_drill.TieredDrill`) asks every tier
 *  with its own canonical, bounds the roots by the tiers' sum against `R + 2·Σ rg` (heaviness is sticky: a
 *  directory past it has a rollup in some tier), combines roots on `(path, usr, vf)` with the smallest `vt`,
 *  and stacks rollups newest first down to a full header (`DRILL_RULES`). */
import { parquetReadObjects, type FileMetaData, type RowGroup } from 'hyparquet'
import { deserializeTCompactProtocol, readVarInt, readZigZag } from 'hyparquet/src/thrift.js'
import type { CatalogMeta, Member } from './staticCatalog.js'
import type { Found, HitCache, HitOpts, HitSource } from './staticFilter.js'
import { type Blobs, cmp, decodeFlat, type FlatSchema, type Hit, type IndexCache, scanAt } from './staticNames.js'
import { subBlobs } from './staticRuns.js'
import { compressors } from './zstd.js'
import { type HexRule, occurs, ruleOf } from './hexRuns.js'
import { isScanId } from '../../src/scanSlug.js'
import { cellsBytes, heldAnswers, heldId, hitsBytes, inFlight, Memo, type Req } from './shared.js'

export const DRILL_DIR = 'drill'

export type Kind = 'long' | 'short'
type Set_ = 'roots' | 'rollups'

export interface DrillMeta {
  gen: string
  R: number
  K: number
  rg: number
  idx_rg: number
  dispatch_rows: number
  /** `'rows'`: a run carrying its roots new to the stack per `q` (`<kind>-fresh.parquet`, `DrillTier.fresh`). */
  fresh?: string
  /** Per set (`long_roots`, …): `row_groups` = the index's entries (the data files' row groups). */
  [set: string]: unknown
}

/** A rollup's cells: per kept child (kind 1) and the remainder (kind 2, `child` ''), the running totals of
 *  the roots at or under it, one cell per change (`vf` epoch ms; a child's value on D is its newest cell
 *  with `vf ≤ D`, zero if none). */
export interface RollupCell { kind: 1 | 2; child: string; vf: number; b: bigint; o: bigint }
export interface Rollup {
  /** The view path whose children these are (`''`: the fleet root, from the catalog). */
  dir: string
  /** Children kept by name. */
  kept: number
  /** Root rows under `dir`, every date (an upper bound on any date's match roots: closed versions and each owner
   *  slice count); at the fleet root a long member's base alias entry, a short one's counted from the base's roots
   *  index (`GroupFile.count`), plus each run's roots new to the stack (`DrillTier.fresh`): exactly the compacted
   *  generation's count. Null: unknown. */
  rows: number | null
  /** Children holding roots, every date. */
  children: number
  cells: RollupCell[]
  /** The fleet root from the catalog alone, no drilldown behind it (`staticFilter.ts` `catalogRoot`): a
   *  bucket's view needs the full index (`FILTER_STATIC_HEAVY`), so drilling in is refused. */
  bucketsOnly?: true
  /** With `bucketsOnly`: a view below answers where the term's rows there are few enough (`^q`'s scoped reads,
   *  `staticAnchors.ts`), and is refused elsewhere. */
  scopedBelow?: true
}

/** A rollup on scan `date`: each kept child's totals (zeros left out, sorted by name) and the remainder's — per
 *  `(kind, child)` its newest cell with `vf ≤ D` (cells from several tiers come in any order). */
export function rollupAt(r: Rollup, date: string): { kids: [string, bigint, bigint][]; rest: [bigint, bigint] } {
  const D = scanAt(date)
  const kids = new Map<string, RollupCell>()
  let rest: RollupCell | null = null
  for (const c of r.cells) {
    if (!(c.vf <= D)) continue
    if (c.kind === 2) { if (!rest || c.vf > rest.vf) rest = c }
    else { const k = kids.get(c.child); if (!k || c.vf > k.vf) kids.set(c.child, c) }
  }
  return {
    kids: [...kids.values()].filter(c => c.b !== 0n || c.o !== 0n).sort((x, y) => cmp(x.child, y.child)).map(c => [c.child, c.b, c.o]),
    rest: rest ? [rest.b, rest.o] : [0n, 0n],
  }
}

/** A rollup's total (kept + remainder) on `date`. */
export function rollupTotal(r: Rollup, date: string): { b: number; o: number } {
  const { kids, rest } = rollupAt(r, date)
  let b = rest[0], o = rest[1]
  for (const [, kb, ko] of kids) { b += kb; o += ko }
  return { b: Number(b), o: Number(o) }
}

// --- decoding -------------------------------------------------------------------------------------

const ROOTS: FlatSchema = { columns: ['q', 'path', 'usr', 'vf', 'vt', 'size', 'n_files'], types: ['BYTE_ARRAY', 'BYTE_ARRAY', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64', 'INT64'] }
const ROLLUPS: FlatSchema = { columns: ['q', 'dir', 'kind', 'child', 'vf', 'b', 'o'], types: ['BYTE_ARRAY', 'BYTE_ARRAY', 'INT32', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64'] }
const SCHEMA: Record<Set_, FlatSchema> = { roots: ROOTS, rollups: ROLLUPS }
const KEY: Record<Set_, 'path' | 'dir'> = { roots: 'path', rollups: 'dir' }

/** An index file's columns as `roots index` and the runs' writer write them (`file` nullable in the base, where the
 *  concatenation re-sets it, and required in a run's: `fileOptional` reads which from the file's footer). */
const INDEX_FIELDS = [
  { name: 'file', type: 'BYTE_ARRAY' }, { name: 'rg', type: 'INT32' },
  { name: 'q_min', type: 'BYTE_ARRAY' }, { name: 'k_min', type: 'BYTE_ARRAY' }, { name: 'q_max', type: 'BYTE_ARRAY' }, { name: 'k_max', type: 'BYTE_ARRAY' },
  { name: 'offset', type: 'INT64' }, { name: 'length', type: 'INT64' }, { name: 'rows', type: 'INT32' }, { name: 'chunks', type: 'INT64', list: true },
] as const

/** Decode one row group of an index file from fetched bytes (no footer: the schema above and the top's chunk offsets). */
async function decodeIndexGroup(rows: number, chunks: number[], buf: ArrayBuffer, start: number, fileOptional: boolean): Promise<Entry[]> {
  const schema: Record<string, unknown>[] = [{ name: 'schema', repetition_type: 'REQUIRED', num_children: INDEX_FIELDS.length }]
  const columns: unknown[] = []
  INDEX_FIELDS.forEach((f, c) => {
    const list = 'list' in f && f.list
    if (list) schema.push({ name: f.name, repetition_type: 'REQUIRED', converted_type: 'LIST', num_children: 1 }, { name: 'list', repetition_type: 'REPEATED', num_children: 1 }, { name: 'element', type: f.type, repetition_type: 'OPTIONAL' })
    else schema.push({ name: f.name, type: f.type, repetition_type: c === 0 && fileOptional ? 'OPTIONAL' : 'REQUIRED', ...(f.type === 'BYTE_ARRAY' ? { converted_type: 'UTF8' } : {}) })
    const dict = chunks[c * 3 + 2]
    columns.push({ meta_data: { type: f.type, path_in_schema: list ? [f.name, 'list', 'element'] : [f.name], codec: 'ZSTD', data_page_offset: BigInt(chunks[c * 3]), total_compressed_size: BigInt(chunks[c * 3 + 1]), ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}) } })
  })
  const metadata = { version: 1, schema, num_rows: BigInt(rows), row_groups: [{ num_rows: BigInt(rows), columns } as unknown as RowGroup], metadata_length: 0 } as unknown as FileMetaData
  const end = start + buf.byteLength
  const file = {
    byteLength: end,
    slice(s: number, e?: number) {
      const to = e ?? end
      if (s < start || to > end) throw new Error(`static drill: index read outside the fetched span: ${s}-${to}`)
      return buf.slice(s - start, to - start)
    },
  }
  const got = await parquetReadObjects({ file, metadata, compressors }) as { file: string; rg: number; q_min: string; k_min: string; q_max: string; k_max: string; offset: bigint; length: bigint; rows: number; chunks: bigint[] }[]
  if (got.length !== rows) throw new Error(`static drill: an index row group decoded ${got.length} entries, expected ${rows}`)
  return got.map(r => ({ file: r.file, rg: r.rg, qMin: r.q_min, kMin: r.k_min, qMax: r.q_max, kMax: r.k_max, offset: num(r.offset), length: num(r.length), rows: num(r.rows), chunks: r.chunks.map(num) }))
}

const num = (v: unknown): number => { const n = Number(v); if (!Number.isSafeInteger(n)) throw new Error(`static drill: bad integer ${v}`); return n }

/** A parquet footer's schema elements (`name`, `repetition_type`: 0 required, 1 optional), from the footer's first
 *  bytes alone: `FileMetaData` opens with its version (field 1) and its schema (field 2, a list of structs), so the
 *  row groups after them (megabytes in the base's index files) are never fetched. */
export function footerSchema(buf: ArrayBuffer): { name: string; repetition: number | undefined }[] {
  const reader = { view: new DataView(buf), offset: 0 }
  let fid = 0
  for (;;) {
    const byte = reader.view.getUint8(reader.offset++)
    const type = byte & 0x0f, delta = byte >> 4
    fid = delta ? fid + delta : readZigZag(reader)
    if (fid === 1 && type === 5) { readZigZag(reader); continue }
    if (fid === 2 && type === 9) {
      const h = reader.view.getUint8(reader.offset++)
      const size = h >> 4 === 15 ? readVarInt(reader) : h >> 4
      if ((h & 0x0f) !== 12) throw new Error('static drill: a footer schema that is not a list of structs')
      const dec = new TextDecoder()
      return Array.from({ length: size }, () => {
        const e = deserializeTCompactProtocol(reader) as { field_3?: number; field_4?: Uint8Array }
        return { name: e.field_4 ? dec.decode(e.field_4) : '', repetition: e.field_3 }
      })
    }
    throw new Error(`static drill: a footer that does not open with its version and schema (field ${fid}, type ${type})`)
  }
}

/** The footer prefix read for its schema (an index file's is ~300 B). */
const SCHEMA_BYTES = 4096

// --- the two-level group index ----------------------------------------------------------------

/** One data row group: its file, exact first and last `(q, key)`, byte span, rows, and column chunks. */
export interface Entry { file: string; rg: number; qMin: string; kMin: string; qMax: string; kMax: string; offset: number; length: number; rows: number; chunks: number[] }
/** A `.top.parquet`, column-wise: per index row group its first and last key, byte span, data rows, chunks. */
export interface Top { qMin: string[]; kMin: string[]; qMax: string[]; kMax: string[]; offset: number[]; length: number[]; rows: number[]; chunks: number[][] }

type Key = readonly [string, string]
const cmpKey = (a: Key, b: Key): number => cmp(a[0], b[0]) || cmp(a[1], b[1])
function bisect(n: number, pred: (i: number) => boolean): number {
  let lo = 0, hi = n
  while (lo < hi) { const m = (lo + hi) >>> 1; if (pred(m)) hi = m; else lo = m + 1 }
  return lo
}
/** Entries `[a, b)` (sorted, disjoint) whose key range meets `[lo, hi)` (`GroupFile._meet`). */
function meet(n: number, min: (i: number) => Key, max: (i: number) => Key, lo: Key, hi: Key): [number, number] {
  const a = bisect(n, i => cmpKey(max(i), lo) >= 0)
  const b = bisect(n, i => cmpKey(min(i), hi) >= 0)
  return [a, Math.max(a, b)]
}

/** What a drill read cost (the `x-static-io`/trace detail). */
export interface DrillIo { top: 'isolate' | 'cache' | 'file'; index_reads: number; index_bytes: number; groups: number; bytes: number; rows_read: number }
const newIo = (): DrillIo => ({ top: 'isolate', index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 })

/** One roots or rollups set of one kind, through its two-level index (`static_roots.GroupFile`). */
export class GroupFile {
  /** The top and whether `file` is nullable, held once read; a read pending in another request is joined, never
   *  awaited as is (`shared.ts`). Every read's `req`: the request reading (`reqOf(env)`). */
  private top = new Memo<Top>('static-drill-top')
  private optional = new Memo<boolean>('static-drill-optional')
  constructor(readonly blobs: Blobs, readonly kind: Kind | 'end' | 'exact' | 'start', readonly set: Set_, readonly meta: (req?: Req) => Promise<DrillMeta>, readonly cache?: IndexCache<Top>) {}

  get indexFile(): string { return `${this.kind}-${this.set}-index.parquet` }
  get topFile(): string { return `${this.kind}-${this.set}-index.top.parquet` }

  /** The top: the isolate's, else the colo cache's, else the file read whole (≤ ~1 MB) and decoded. */
  private async loadTop(io: DrillIo, req?: Req): Promise<Top> {
    return this.top.get('', async () => {
      const cached = await this.cache?.get(this.topFile)
      if (cached) { io.top = 'cache'; return cached }
      io.top = 'file'
      const buf = await this.blobs.range(this.topFile, 0)
      const rows = await parquetReadObjects({ file: buf, compressors }) as { rg: number; q_min: string; k_min: string; q_max: string; k_max: string; offset: bigint; length: bigint; rows: bigint; chunks: bigint[] }[]
      rows.sort((x, y) => x.rg - y.rg)
      if (rows.some((r, i) => r.rg !== i)) throw new Error(`static drill: ${this.topFile} row groups are not 0..n`)
      const top: Top = { qMin: [], kMin: [], qMax: [], kMax: [], offset: [], length: [], rows: [], chunks: [] }
      for (const r of rows) {
        top.qMin.push(r.q_min); top.kMin.push(r.k_min); top.qMax.push(r.q_max); top.kMax.push(r.k_max)
        top.offset.push(num(r.offset)); top.length.push(num(r.length)); top.rows.push(num(r.rows)); top.chunks.push(r.chunks.map(num))
      }
      await this.cache?.put(this.topFile, top)
      return top
    }, req)
  }

  /** Whether the index file's `file` column is nullable (its footer's schema; isolate-held). */
  private fileOptional(req?: Req): Promise<boolean> {
    return this.optional.get('', async () => {
      const { buf, size } = await this.blobs.suffix(this.indexFile, 8)
      const len = new DataView(buf).getUint32(0, true)
      const head = await this.blobs.range(this.indexFile, size - 8 - len, Math.min(len, SCHEMA_BYTES))
      const f = footerSchema(head).find(e => e.name === 'file')
      if (!f || (f.repetition !== 0 && f.repetition !== 1)) throw new Error(`static drill: ${this.indexFile} has no file column`)
      return f.repetition === 1
    }, req)
  }

  /** Entries in index row group `g` (every one holds `idx_rg` but the last). */
  private async entriesIn(g: number, n: number, req?: Req): Promise<number> {
    const m = await this.meta(req)
    const total = (m[`${this.kind}_${this.set}`] as { row_groups?: number } | undefined)?.row_groups
    if (!Number.isSafeInteger(total)) throw new Error(`static drill: meta.json has no ${this.kind}_${this.set}.row_groups`)
    return g < n - 1 ? m.idx_rg : total! - m.idx_rg * (n - 1)
  }

  /** The data row groups meeting `[lo, hi)` and their rows' sum. With `cap`, when the index row groups strictly
   *  inside the range already hold more than `cap` rows, `groups` is null (the range is at least that big)
   *  and the index file is not read. */
  async select(lo: Key, hi: Key, io: DrillIo, cap?: number, req?: Req): Promise<{ groups: Entry[] | null; rows: number }> {
    const top = await this.loadTop(io, req)
    const n = top.qMin.length
    const [a, b] = meet(n, i => [top.qMin[i], top.kMin[i]], i => [top.qMax[i], top.kMax[i]], lo, hi)
    if (a === b) return { groups: [], rows: 0 }
    let inner = 0
    for (let i = a + 1; i < b - 1; i++) inner += top.rows[i]
    if (cap != null && inner > cap) return { groups: null, rows: inner }
    const start = top.offset[a], end = top.offset[b - 1] + top.length[b - 1]
    const [buf, optional] = await Promise.all([this.blobs.range(this.indexFile, start, end - start), this.fileOptional(req)])
    io.index_reads++
    io.index_bytes += buf.byteLength
    const entries: Entry[] = []
    for (let g = a; g < b; g++) entries.push(...await decodeIndexGroup(await this.entriesIn(g, n, req), top.chunks[g], buf, start, optional))
    const [x, y] = meet(entries.length, i => [entries[i].qMin, entries[i].kMin], i => [entries[i].qMax, entries[i].kMax], lo, hi)
    const groups = entries.slice(x, y)
    return { groups, rows: groups.reduce((s, e) => s + e.rows, 0) }
  }

  /** The exact number of rows with `lo ≤ (q, key) < hi`, however many: the index row groups strictly inside
   *  the range by their top `rows`, the two at its edges read (their entries fully inside by `rows`), and the
   *  at most two data groups straddling an edge decoded. A short literal's root count at the fleet root
   *  (`.`: 361M rows) for ~2 small index reads. */
  async count(lo: Key, hi: Key, io: DrillIo, req?: Req): Promise<number> {
    const top = await this.loadTop(io, req)
    const n = top.qMin.length
    const [a, b] = meet(n, i => [top.qMin[i], top.kMin[i]], i => [top.qMax[i], top.kMax[i]], lo, hi)
    if (a === b) return 0
    let rows = 0
    for (let i = a + 1; i < b - 1; i++) rows += top.rows[i]
    const inside = (e: Entry) => cmpKey([e.qMin, e.kMin], lo) >= 0 && cmpKey([e.qMax, e.kMax], hi) < 0
    const partial: Entry[] = []
    for (const g of b - 1 > a ? [a, b - 1] : [a]) {
      const start = top.offset[g]
      const [buf, optional] = await Promise.all([this.blobs.range(this.indexFile, start, top.length[g]), this.fileOptional(req)])
      io.index_reads++
      io.index_bytes += buf.byteLength
      const entries = await decodeIndexGroup(await this.entriesIn(g, n, req), top.chunks[g], buf, start, optional)
      const [x, y] = meet(entries.length, i => [entries[i].qMin, entries[i].kMin], i => [entries[i].qMax, entries[i].kMax], lo, hi)
      for (const e of entries.slice(x, y)) { if (inside(e)) rows += e.rows; else partial.push(e) }
    }
    // Each straddling group read on its own: the two edges of a big range lie far apart in one file, and `read`
    // fetches a file's groups as one span (606 MB for `4`'s base roots).
    for (const n of await Promise.all(partial.map(e => this.read(lo, hi, [e], io, () => 1)))) rows += n.length
    return rows
  }

  /** The rows of `groups` with `lo ≤ (q, key) < hi`: one ranged GET per data file. `row` makes each one from
   *  the decoded columns (default: an object of every column). */
  async read<T = Record<string, unknown>>(lo: Key, hi: Key, groups: Entry[], io: DrillIo, row?: (cols: Record<string, unknown[]>, i: number) => T): Promise<T[]> {
    const spec = SCHEMA[this.set], key = KEY[this.set]
    const byFile = new Map<string, Entry[]>()
    for (const e of groups) byFile.set(e.file, [...(byFile.get(e.file) ?? []), e])
    io.groups += groups.length
    const parts = await Promise.all([...byFile].map(async ([file, gs]) => {
      const start = gs[0].offset, end = gs[gs.length - 1].offset + gs[gs.length - 1].length
      const buf = await this.blobs.range(file, start, end - start)
      io.bytes += buf.byteLength
      const out: T[] = []
      const make = row ?? ((cols: Record<string, unknown[]>, i: number) => Object.fromEntries(spec.columns.map(c => [c, cols[c][i]])) as T)
      for (const g of gs) {
        const cols = await decodeFlat(spec, start + buf.byteLength, g.rows, g.chunks, buf, start)
        io.rows_read += g.rows
        for (let i = 0; i < g.rows; i++) {
          const q = cols.q[i] as string, k = cols[key][i] as string
          if ((cmp(q, lo[0]) || cmp(k, lo[1])) < 0 || (cmp(q, hi[0]) || cmp(k, hi[1])) >= 0) continue
          out.push(make(cols, i))
        }
      }
      return out
    }))
    return parts.flat()
  }
}

// --- one tier -----------------------------------------------------------------------------------

/** A tier's colo caches: its index tops and its alias map (`[q, canonical ('' = itself), n (−1 = unknown)]`). */
export interface TierCache { top?: IndexCache<Top>; aliases?: IndexCache<[string[], string[], number[]]> }

/** One tier's drill: the base generation's `drill/` (`dir` null) or a run's `<dir>/drill/`, in the same layout. */
export class DrillTier {
  /** `meta.json`, the alias map and the fresh counts, held once read; one pending in another request is joined (`shared.ts`). */
  private meta = new Memo<DrillMeta>('static-drill-meta')
  private aliasMap = new Memo<Map<string, { c: string; n: number | null }>>('static-drill-aliases')
  private freshMap = new Memo<Map<string, number>>('static-drill-fresh')
  readonly files: Record<Kind, Record<Set_, GroupFile>>
  constructor(readonly blobs: Blobs, readonly dir: string | null, readonly cache?: TierCache) {
    const meta = (req?: Req) => this.info(req)
    const gf = (k: Kind, s: Set_) => new GroupFile(blobs, k, s, meta, cache?.top)
    this.files = { long: { roots: gf('long', 'roots'), rollups: gf('long', 'rollups') }, short: { roots: gf('short', 'roots'), rollups: gf('short', 'rollups') } }
  }

  /** `meta.json` (isolate-held). `req`: the request asking (`reqOf(env)`). */
  info(req?: Req): Promise<DrillMeta> {
    return this.meta.get('', () => this.blobs.json<DrillMeta>('meta.json').then(m => {
      for (const k of ['R', 'K', 'rg', 'idx_rg', 'dispatch_rows'] as const) if (!Number.isSafeInteger(m[k]) || m[k] < 1) throw new Error(`static drill: bad meta.json ${k}`)
      return m
    }), req)
  }

  /** Every long member of the tier → its canonical (itself, or the member whose root set it shares there) and,
   *  where the tier records it (the base), its root rows. */
  aliases(req?: Req): Promise<Map<string, { c: string; n: number | null }>> {
    return this.aliasMap.get('', async () => {
      const cached = await this.cache?.aliases?.get('aliases.parquet')
      if (cached) return new Map(cached[0].map((q, i) => [q, { c: cached[1][i] || q, n: cached[2][i] < 0 ? null : cached[2][i] }]))
      const rows = await parquetReadObjects({ file: await this.blobs.range('aliases.parquet', 0), compressors }) as { q: string; canonical: string; n?: bigint }[]
      const m = new Map(rows.map(r => [r.q, { c: r.canonical, n: r.n == null ? null : num(r.n) }]))
      await this.cache?.aliases?.put('aliases.parquet', [[...m.keys()], [...m].map(([q, a]) => a.c === q ? '' : a.c), [...m.values()].map(a => a.n ?? -1)])
      return m
    }, req)
  }

  /** A run's roots new to the stack per class canonical (long) or literal (short), `<kind>-fresh.parquet` (`static_drill.
   *  write_fresh`: an existing member's opens, every root of a new member; absent = none); null: a tier without them
   *  (`meta.json` `fresh`, a run built before them). */
  async fresh(kind: Kind, req?: Req): Promise<Map<string, number> | null> {
    if ((await this.info(req)).fresh !== 'rows') return null
    return this.freshMap.get(kind, async () => {
      const rows = await parquetReadObjects({ file: await this.blobs.range(`${kind}-fresh.parquet`, 0), compressors }) as { q: string; n: bigint }[]
      return new Map(rows.map(r => [r.q, num(r.n)]))
    }, req)
  }
}

// --- the tiers' rules ---------------------------------------------------------------------------

type RollupRow = Record<string, unknown>
const KIND_FULL = 0, KIND_DELTA = -1

/** How the tiers' rows make one answer (specs/static-daily-append.md, "Reader (base ⊕ runs)"). A parameter only
 *  so the tests can break each rule and see the views go wrong; `DRILL_RULES` is the reader's. */
export interface DrillRules {
  /** A literal's canonical in tier `i` (`maps[i]` its alias map): `aliases_i[t] ?? t`, each tier its own. */
  canon(maps: Map<string, { c: string }>[], i: number, t: string): string
  /** The dispatch bound over the tiers' metas (base first): `R + 2·Σ rg`, since each tier's bound is at most its
   *  rows plus two of its row groups. */
  bound(metas: DrillMeta[]): number
  /** The tiers' match roots (base first) as one set: rows equal on `(path, usr, vf)` are one root, the smallest
   *  `vt` wins (a version's `vt` moves once, from open to its closing scan). */
  combine(parts: Hit[][]): Hit[]
  /** The tiers' rollup rows for one `(c_i, P)` (base first; each tier's first row its header) as the newest header
   *  and the cells: newest tier first, each tier's cells, stopping after a full header (`kind` 0: the whole history;
   *  a delta header, `kind` −1, adds the cells dated in its run). */
  stack(parts: RollupRow[][]): { head: RollupRow; cells: RollupRow[] } | null
}

export const DRILL_RULES: DrillRules = {
  canon: (maps, i, t) => maps[i].get(t)?.c ?? t,
  bound: metas => metas[0].R + 2 * metas.reduce((s, m) => s + m.rg, 0),
  combine(parts) {
    const some = parts.filter(p => p.length)
    if (some.length <= 1) return some[0] ?? []
    const out: Hit[] = []
    const at = new Map<string, number>()
    for (const p of some) for (const h of p) {
      const k = `${h.path}\0${h.usr}\0${h.vf}`
      const i = at.get(k)
      if (i == null) { at.set(k, out.length); out.push(h) } else if (h.vt < out[i].vt) out[i] = h
    }
    return out
  },
  stack(parts) {
    let head: RollupRow | null = null
    const cells: RollupRow[] = []
    for (let i = parts.length - 1; i >= 0; i--) {
      const rows = parts[i]
      if (!rows.length) continue
      if (rows[0].kind !== KIND_FULL && rows[0].kind !== KIND_DELTA) throw new Error(`static drill: rollup rows without a header (tier ${i})`)
      head ??= rows[0]
      cells.push(...rows.slice(1))
      if (rows[0].kind === KIND_FULL) break
    }
    return head ? { head, cells } : null
  },
}

// --- the drill: base ⊕ runs ---------------------------------------------------------------------

/** A read of one tier's files failed (missing or corrupt meta, index or data): `DrillSource` cuts the stack there
 *  and asks again over the tiers before it. */
export class TierError extends Error {
  constructor(readonly dir: string | null, readonly cause: unknown) {
    super(`static drill: tier ${dir ?? 'base'}: ${cause instanceof Error ? cause.message : String(cause)}`)
    this.name = 'TierError'
  }
}

/** `f` over the tiers, a failure raised as the lowest failing tier's `TierError`. */
async function perTier<T>(tiers: DrillTier[], f: (x: DrillTier, i: number) => Promise<T>): Promise<T[]> {
  const got = await Promise.allSettled(tiers.map((x, i) => f(x, i)))
  const bad = got.findIndex(g => g.status === 'rejected')
  if (bad >= 0) { const e = (got[bad] as PromiseRejectedResult).reason; throw e instanceof TierError ? e : new TierError(tiers[bad].dir, e) }
  return got.map(g => (g as PromiseFulfilledResult<T>).value)
}

/** A view's answer, any scan the tiers cover: the match roots under P (≤ the dispatch bound), or P's rollup. */
export type DrillAnswer =
  | { source: 'plain' }
  /** A long literal that is no member (the light reader's, or nobody's). */
  | { source: 'none' }
  | { source: 'roots'; kind: Kind; c: string; upper: number; hits: Hit[]; io: DrillIo }
  /** `scans`: narrower than the state's when the catalog's stack was cut (`CatalogLookup`). */
  | { source: 'rollup' | 'catalog'; kind: Kind; c: string; upper: number; rollup: Rollup; io: DrillIo; scans?: string[] }
/** Roots bounded at more than the caller's `maxRows` (`Drill.bounded`): not read. */
export interface DrillOver { source: 'over'; upper: number }

/** The catalog's per-member lookup (the fleet root's rollup): the base's, or the base's and its runs' (`scans`: the
 *  ones its cells are exact on, when its own stack was cut at a broken tier). */
export interface CatalogLookup { lookup(q: string, opts?: { req?: Req }): Promise<{ member: Member | null; scans?: string[] }>; info?(req?: Req): Promise<CatalogMeta> }

/** The live tiers: the base and the runs whose drill is there, oldest first, up to the first broken one; the scans
 *  they cover; and a version naming the newest tier (`base`, or its run's directory; `none` with no usable tier),
 *  which versions the answers spanning every scan (hit lists). */
export interface DrillState { version: string; scans: string[]; tiers: DrillTier[] }

export interface DrillOpts {
  /** The generation's runs (their directories, oldest first: the light index's tiers past the base,
   *  `staticRuns.ts`). A run is a drill tier iff its `drill/meta.json` exists (written last); the tiers are the
   *  base and the runs up to the first one without it. Absent: the base alone. */
  runs?: (req?: Req) => Promise<string[]>
  /** A tier's colo caches (`dir` null: the base). */
  cache?: (dir: string | null) => TierCache | undefined
  rules?: DrillRules
  /** The live tiers are re-listed at most this often (a tier's readers, and its isolate caches, are kept), and
   *  whenever `runs` changes. */
  ttlMs?: number
  /** A tier whose read failed stays out this long; a state cut at one is re-built after it. */
  brokenTtlMs?: number
  now?: () => number
  log?: (msg: string) => void
}

/** The drilldown over a generation (`blobs` keyed under it): the base `drill/` plus each run's `<run>/drill/`. */
export class Drill {
  private held = new Map<string, DrillTier>()
  private live = new Set<string>()
  private broken = new Map<string, number>()
  /** The last good state (`ok`, built at `at` over the runs `runsAt`; `cut` at a broken tier) and the refresh in
   *  flight, shared across requests as an owned, tracked entry (`shared.ts` `inFlight`; keyed by the runs). `epoch`:
   *  bumped by `broke`, so a refresh started before it neither is joined nor lands. */
  private ok?: DrillState
  private at = -Infinity
  private cut = false
  private runsAt = ''
  private epoch = 0
  private refreshing = new Map<string, Promise<DrillState>>()
  readonly rules: DrillRules
  constructor(readonly blobs: Blobs, readonly catalog: CatalogLookup | null, readonly opts: DrillOpts = {}) {
    this.rules = opts.rules ?? DRILL_RULES
  }

  private tier(dir: string | null): DrillTier {
    const k = dir ?? ''
    let t = this.held.get(k)
    if (!t) { t = new DrillTier(subBlobs(this.blobs, dir == null ? DRILL_DIR : `${dir}/${DRILL_DIR}`), dir, this.opts.cache?.(dir)); this.held.set(k, t) }
    return t
  }

  private now(): number { return this.opts.now?.() ?? Date.now() }

  /** Whether run `dir`'s drill is live (its `meta.json` listed; once live, always). */
  private async isLive(dir: string): Promise<boolean> {
    if (this.live.has(dir)) return true
    if (!this.blobs.list) return false
    const key = `${dir}/${DRILL_DIR}/meta.json`
    const yes = (await this.blobs.list(key)).includes(key)
    if (yes) this.live.add(dir)
    return yes
  }

  /** Record tier `dir` as broken, logged with the error (which names the key) once per `brokenTtlMs`. */
  private mark(dir: string | null, error: unknown): void {
    const t = this.now(), was = this.broken.get(dir ?? '')
    if (was != null && t - was < (this.opts.brokenTtlMs ?? 30_000)) return
    this.broken.set(dir ?? '', t)
    const msg = error instanceof TierError ? error.cause : error
    ;(this.opts.log ?? console.error)(`static drill: ${dir ?? 'the base'} is broken; its scans and every later tier's are not indexed until it loads: ${msg instanceof Error ? msg.message : String(msg)}`)
  }

  /** A read of tier `dir` failed: the next `state()` is cut at it. */
  broke(dir: string | null, error: unknown): void {
    this.mark(dir, error)
    this.ok = undefined
    this.at = -Infinity
    this.epoch++
    this.refreshing.clear()
  }

  /** Whether `x` is usable at `t`: not broken within `brokenTtlMs`, and its `meta.json` loads (a run's listing its
   *  scans, returned). */
  private async usable(x: DrillTier, t: number, req?: Req): Promise<string[] | null> {
    const b = this.broken.get(x.dir ?? '')
    if (b != null && t - b < (this.opts.brokenTtlMs ?? 30_000)) return null
    try {
      const m = await x.info(req)
      if (x.dir != null && (!Array.isArray(m.scans) || !m.scans.length)) throw new Error(`static drill: ${x.dir}/${DRILL_DIR}/meta.json lists no scans`)
      this.broken.delete(x.dir ?? '')
      return x.dir == null ? [] : m.scans as string[]
    } catch (e) {
      this.mark(x.dir, e)
      return null
    }
  }

  /** The live tiers, re-listed once `ttlMs` old (`brokenTtlMs` when cut) or the runs changed. A refresh another
   *  request has in flight is joined; a failed one is the caller's error, and the next call refreshes again. `req`:
   *  the request asking (`reqOf(env)`). */
  async state(req?: Req): Promise<DrillState> {
    const dirs = await (this.opts.runs?.(req) ?? [])
    const runsAt = dirs.join('\n')
    const t = this.now()
    const ttl = this.opts.ttlMs ?? 300_000
    if (this.ok && runsAt === this.runsAt && t - this.at < (this.cut ? Math.min(ttl, this.opts.brokenTtlMs ?? 30_000) : ttl)) return this.ok
    const epoch = this.epoch
    return inFlight(this.refreshing, runsAt, async () => {
      const { st, cut } = await this.load(dirs, t, req)
      if (this.epoch === epoch) { this.ok = st; this.at = t; this.cut = cut; this.runsAt = runsAt }
      return st
    }, { name: 'static-drill-state', req })
  }

  /** The state over the runs `dirs` as of `t`: the base and the live runs, cut at the first unusable one (`cut`). */
  private async load(dirs: string[], t: number, req?: Req): Promise<{ st: DrillState; cut: boolean }> {
    let cut = true
    const st = await (async (): Promise<DrillState> => {
      const none: DrillState = { version: 'none', scans: [], tiers: [] }
      if (!await this.usable(this.tier(null), t, req)) return none
      let scans: string[]
      try {
        scans = (await this.blobs.json<{ scans: { id: string }[] }>('scans.json')).scans.map(s => s.id)
      } catch (e) {
        this.mark(null, e)
        return none
      }
      const live = await Promise.all(dirs.map(d => this.isLive(d)))
      const n = live.indexOf(false)
      const cands = (n < 0 ? dirs : dirs.slice(0, n)).map(d => this.tier(d))
      const got = await Promise.all(cands.map(r => this.usable(r, t, req)))
      const k = got.indexOf(null)
      const runs = k < 0 ? cands : cands.slice(0, k)
      cut = k >= 0
      for (let i = 0; i < runs.length; i++) scans.push(...got[i]!)
      const bad = scans.find(s => !isScanId(s))
      if (bad != null) throw new Error(`static drill: bad scan id ${JSON.stringify(bad)}`)
      return { version: runs.length ? runs[runs.length - 1].dir! : 'base', scans: scans.sort(), tiers: [this.tier(null), ...runs] }
    })()
    return { st, cut }
  }

  /** The base tier's `meta.json`. */
  info(req?: Req): Promise<DrillMeta> { return this.tier(null).info(req) }
  /** The base tier's alias map. */
  aliases(req?: Req): Promise<Map<string, { c: string; n: number | null }>> { return this.tier(null).aliases(req) }

  /** The generation's hex-run rule (`hexRuns.ts`): the catalog's `meta.json` `hex_runs` (null without a catalog
   *  `info`, or on a generation without the rule). */
  async hexRuns(req?: Req): Promise<HexRule | null> {
    return this.catalog?.info ? ruleOf((await this.catalog.info(req)).hex_runs) : null
  }

  /** `t`'s match root at exactly `path` on the tiers of `state` (every version of every owner slice; none when
   *  `path` is no root of `t`), any scan: one key range `[(c_i, path), (c_i, path + U+0000))` of each tier's roots,
   *  combined as `view`'s are. Null: a long literal that is no member. */
  async rootAt(t: string, path: string, state?: DrillState, req?: Req): Promise<Hit[] | null> {
    const st = state ?? await this.state(req)
    const { tiers } = st
    const kind: Kind = [...t].length <= 2 ? 'short' : 'long'
    let cs = tiers.map(() => t)
    if (kind === 'long') {
      const maps = await perTier(tiers, x => x.aliases(req))
      if (!maps[maps.length - 1].has(t)) return null
      cs = tiers.map((_, i) => this.rules.canon(maps, i, t))
    }
    const io = newIo()
    const lo = (c: string): Key => [c, path], hi = (c: string): Key => [c, path + '\0']
    const sels = await perTier(tiers, (x, i) => x.files[kind].roots.select(lo(cs[i]), hi(cs[i]), io, undefined, req))
    const parts = await perTier(tiers, async (x, i) => sels[i].groups?.length ? x.files[kind].roots.read<Hit>(lo(cs[i]), hi(cs[i]), sels[i].groups!, io, hitOf) : [])
    return this.rules.combine(parts)
  }

  /** `t`'s (lowercase) view at `P`, on any scan of `state` (default: the current one). */
  view(t: string, P: string, state?: DrillState, req?: Req): Promise<DrillAnswer> { return this.bounded(t, P, state, undefined, req) as Promise<DrillAnswer> }

  /** `view`, except roots bounded past `maxRows` are not read (`over`). */
  async bounded(t: string, P: string, state?: DrillState, maxRows?: number, req?: Req): Promise<DrillAnswer | DrillOver> {
    if (occurs(t, P.toLowerCase(), await this.hexRuns(req))) return { source: 'plain' }
    const st = state ?? await this.state(req)
    const { tiers } = st
    const kind: Kind = [...t].length <= 2 ? 'short' : 'long'
    const R = this.rules
    let cs = tiers.map(() => t)
    let all: number | null = null
    if (kind === 'long') {
      const maps = await perTier(tiers, x => x.aliases(req))
      // A member iff the newest tier's map holds it (each run's map lists every member as of its scan).
      if (!maps[maps.length - 1].has(t)) return { source: 'none' }
      cs = tiers.map((_, i) => R.canon(maps, i, t))
      // The base's root rows of `t` (its alias entry; 0 when `t` became a member in a run).
      const b = maps[0].get(t)
      all = b ? b.n : 0
    }
    const metas = await perTier(tiers, x => x.info(req))
    const thr = R.bound(metas)
    const io = newIo()
    // The fleet root: the whole member, `[(c, ''), (c + U+0000, ''))`.
    const lo = (c: string): Key => P === '' ? [c, ''] : [c, P + '/']
    const hi = (c: string): Key => P === '' ? [c + '\0', ''] : [c, P + '0']
    const sels = await perTier(tiers, (x, i) => x.files[kind].roots.select(lo(cs[i]), hi(cs[i]), io, thr, req))
    const upper = sels.reduce((s, x) => s + x.rows, 0)
    if (sels.every(s => s.groups) && upper <= thr) {
      if (maxRows != null && upper > maxRows) return { source: 'over', upper }
      // Hits straight from the columns (no row object per root: a member's ~80K roots, decoded per isolate).
      const parts = await perTier(tiers, async (x, i) => sels[i].groups!.length ? x.files[kind].roots.read<Hit>(lo(cs[i]), hi(cs[i]), sels[i].groups!, io, hitOf) : [])
      const c = cs[cs.length - 1]
      return { source: 'roots', kind, c, upper, hits: R.combine(parts), io }
    }
    if (P === '') {
      // No rollup at the fleet root (heavy directories are depth ≥ 1): the catalog's per-bucket cells.
      const got = await this.catalog?.lookup(t, { req })
      if (!got?.member) throw new Error(`static drill: ${JSON.stringify(t)} has ${upper} root rows but no catalog cells`)
      const cells: RollupCell[] = got.member.cells.map(x => ({ kind: 1, child: x.bucket, vf: Number(x.vf) * 1000, b: x.b, o: x.o }))
      const buckets = new Set(cells.map(x => x.child)).size
      // Root rows, every date: a long member's base alias entry (else, and a short one's, counted from the base's
      // roots index), plus each run's roots new to the stack — never its stored rows, whose close records restate roots
      // an earlier tier holds (gcs 10-24: `.parquet` 103.3M over `2026-10-08c` + runs, 94.5M over its compaction). A run
      // built before its fresh counts adds its stored rows: an upper bound.
      const counts = await perTier(tiers, async (x, i) => {
        if (i === 0 && all != null) return all
        const fresh = i === 0 ? null : await x.fresh(kind, req)
        return fresh ? fresh.get(cs[i]) ?? 0 : x.files[kind].roots.count(lo(cs[i]), hi(cs[i]), io, req)
      })
      const rows = counts.reduce((s, x) => s + x, 0)
      // The catalog's own stack cut at a broken tier: its cells answer only the scans it still covers.
      const narrow = got.scans && st.scans.some(d => !got.scans!.includes(d)) ? { scans: st.scans.filter(d => got.scans!.includes(d)) } : {}
      return { source: 'catalog', kind, c: cs[cs.length - 1], upper, rollup: { dir: '', kept: buckets, rows, children: buckets, cells }, io, ...narrow }
    }
    const parts = await perTier(tiers, async (x, i) => {
      const rlo: Key = [cs[i], P], rhi: Key = [cs[i], P + '\0']
      const rs = await x.files[kind].rollups.select(rlo, rhi, io, undefined, req)
      return rs.groups?.length ? x.files[kind].rollups.read(rlo, rhi, rs.groups, io) : []
    })
    const got = R.stack(parts)
    if (!got) throw new Error(`static drill: (${JSON.stringify(t)}, ${JSON.stringify(P)}) bounds ${upper} root rows but has no rollup`)
    const { head } = got
    const cells: RollupCell[] = got.cells.map(r => ({ kind: r.kind as 1 | 2, child: r.child as string, vf: Number(r.vf) * 1000, b: r.b as bigint, o: r.o as bigint }))
    return { source: 'rollup', kind, c: cs[cs.length - 1], upper, rollup: { dir: P, kept: num(head.vf), rows: num(head.b), children: num(head.o), cells }, io }
  }
}

/** A roots row as a hit (epoch seconds → ms). */
function hitOf(c: Record<string, unknown[]>, i: number): Hit {
  const path = c.path[i] as string
  let depth = 1
  for (let j = path.indexOf('/'); j !== -1; j = path.indexOf('/', j + 1)) depth++
  return { path, depth, usr: c.usr[i] as string, vf: Number(c.vf[i]) * 1000, vt: Number(c.vt[i]) * 1000, size: c.size[i] as bigint, n: c.n_files[i] as bigint }
}

/** The filter's heavy source over a drill: a view's match roots, or its rollup, exact on the live tiers' scans
 *  (`Found.scans`). Plain views and non-members decline (null): the caller reads as before. */
export class DrillSource implements HitSource {
  /** Answers held per isolate (an `over` one never), their sizes; one pending in another request is joined
   *  (`shared.ts`). */
  private held = new Memo<DrillAnswer | DrillOver>('static-drill-answer')
  private id = heldId('static-drill-answer')
  /** `cache`: roots answers across isolates (the colo's Cache API) — decoding a member's ~80K roots from the
   *  drill's parquet is seconds of an edge isolate's CPU, each new isolate again. */
  constructor(readonly drill: Drill, readonly cache?: HitCache) {}

  /** An answer's key: a hit list spans every scan, so past the base it is versioned by the newest tier. */
  private static key(t: string, P: string, st: DrillState): string {
    return st.version === 'base' ? `${t}\0${P}` : `${t}\0${P}@${st.version}`
  }

  /** `t`'s view at `P` on the tiers of `st` (isolate-held; `maxRows`: one not held yet whose roots are bounded past
   *  it is `over`, and not held). */
  async answer(t: string, P: string, st: DrillState, maxRows?: number, req?: Req): Promise<DrillAnswer | DrillOver> {
    const k = DrillSource.key(t, P, st)
    const a = await this.held.get(k, () => this.drill.bounded(t, P, st, maxRows, req), req, x => x.source !== 'over')
    if (this.held.has(k)) this.hold(k, a)
    return a
  }

  /** `k`'s answer held (`heldAnswers`, under the isolate's bound for the static sources' answers). */
  private hold(k: string, a: DrillAnswer | DrillOver): void {
    heldAnswers.keep(this.id + k, () => a.source === 'roots' ? hitsBytes(a.hits) : 'rollup' in a ? cellsBytes(a.rollup.cells) : 0, () => this.held.delete(k))
  }

  /** `key`'s view under `under`, exact on the live tiers' scans. A tier that fails to load is cut (`Drill.broke`)
   *  and the view asked again over the tiers before it; no usable tier at all declines (null). */
  async hits(key: string, under: string, opts?: HitOpts, req?: Req): Promise<Found | null> {
    const t0 = Date.now()
    for (let tries = 0; ; tries++) {
      const st = await this.drill.state(req)
      if (!st.tiers.length) return null
      try {
        return await this.hitsOn(key, under, st, t0, opts, req)
      } catch (e) {
        if (!(e instanceof TierError) || tries > st.tiers.length) throw e
        this.drill.broke(e.dir, e)
      }
    }
  }

  /** `key`'s root at exactly `path` (its hits; empty when `path` is none of its roots), exact on the live tiers'
   *  scans; null: no usable tier, or a long literal that is no member. */
  async at(key: string, path: string, req?: Req): Promise<Found | null> {
    const t0 = Date.now()
    for (let tries = 0; ; tries++) {
      const st = await this.drill.state(req)
      if (!st.tiers.length) return null
      try {
        const hits = await this.drill.rootAt(key, path, st, req)
        return hits && { hits, io: { from: 'drill', source: 'at', tiers: st.tiers.length, ms: Date.now() - t0 }, scans: st.scans }
      } catch (e) {
        if (!(e instanceof TierError) || tries > st.tiers.length) throw e
        this.drill.broke(e.dir, e)
      }
    }
  }

  private async hitsOn(key: string, under: string, st: DrillState, t0: number, opts?: HitOpts, req?: Req): Promise<Found | null> {
    const k = DrillSource.key(key, under, st)
    if (this.cache && !this.held.held(k)) {
      const hits = await this.cache.get(k)
      if (hits) {
        if (opts?.maxRows != null && hits.length > opts.maxRows) { opts.over = true; return null }
        const kind: Kind = [...key].length <= 2 ? 'short' : 'long'
        const a: DrillAnswer = { source: 'roots', kind, c: key, upper: hits.length, hits, io: newIo() }
        this.held.set(k, a)
        this.hold(k, a)
        return { hits, io: { from: 'drill', source: 'roots', cache: 'colo', tiers: st.tiers.length, ms: Date.now() - t0 }, scans: st.scans }
      }
    }
    const fresh = !this.held.held(k)
    const a = await this.answer(key, under, st, opts?.maxRows, req)
    // Over the caller's rows: bounded so before the read, or held with that many roots.
    if (a.source === 'over' || (a.source === 'roots' && opts?.maxRows != null && a.hits.length > opts.maxRows)) { opts!.over = true; return null }
    if (fresh && a.source === 'roots' && this.cache) await this.cache.put(k, a.hits).catch(() => {})
    if (a.source === 'plain' || a.source === 'none') return null
    // An answer narrowed by the catalog's broken tier is not held: once that tier loads, the view widens again.
    if ('scans' in a && a.scans) { this.held.delete(k); heldAnswers.forget(this.id + k) }
    const io = { from: 'drill', source: a.source, kind: a.kind, c: a.c, upper: a.upper, tiers: st.tiers.length, ...a.io, ms: Date.now() - t0 }
    return a.source === 'roots' ? { hits: a.hits, io, scans: st.scans } : { rollup: a.rollup, io, scans: a.scans ?? st.scans }
  }
}
