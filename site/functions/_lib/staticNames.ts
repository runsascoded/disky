/** Static name search (specs/architecture/static-name-search.md): answer a literal's per-bucket first-hit
 *  totals from the suffix shards on R2 alone — no D1, no footer parse on the hot path.
 *
 *  Layout (`dt-cloud static-names`, one generation under `static-names/<gen>/`): `sx/s####.parquet`, one
 *  row per (lowercase name suffix of ≥ 3 characters, version) — `s, depth, path, usr, vf, vt, size,
 *  n_files` — sorted by `s`, 8K-row groups, zstd; `shards.json`, the shard plan: shard `i` holds the
 *  suffixes whose first three characters are in `[lo, hi)` (a 3-character prefix is never split, so a
 *  literal's rows are one contiguous run of one file).
 *
 *  A query: `shards.json` (isolate-cached) names the shard → that shard's group index (per row group:
 *  `s` min/max and each column chunk's offsets, cut from the shard's footer once and kept in the isolate
 *  and the colo's Cache API, so warm queries never touch the footer) → the groups whose `s` range can
 *  hold suffixes starting with the key → ONE ranged GET of their bytes → hyparquet decodes them through
 *  a FileMetaData built from the index → the first-hit filter and per-bucket sums for each date.
 *
 *  Exactly `static_names.Reader.answer`: the key is the lowercased literal, untruncated; groups `[a, b)`
 *  with `a` = first group whose `s_max ≥ key`, `b` = first whose `s_min ≥ key + U+10FFFF` (code-point
 *  order, as the shards are sorted); rows whose `s` starts with the key and whose lowercase basename
 *  contains it; per date `D` (epoch ms of the scan id): live `vf ≤ D < vt`, depth ≥ 1, deduped by
 *  `(path, usr, vf)` (a name holding the key twice has two suffix rows), dropped when the lowercase
 *  parent path contains the key (an ancestor already covers it); `size`/`n_files` summed per first
 *  path segment. On a generation built with the hex-run rule (`hexRuns.ts`, its `catalog/meta.json`
 *  `hex_runs`), "contains" is `occurs` under the rule, in the name test and the parent test alike. */
import { type FileMetaData, parquetRead, type RowGroup } from 'hyparquet'
import { isScanId, scanTime } from '../../src/scanSlug.js'
import { compressors } from './zstd.js'
import { firstOccurrence, type HexRule, occurs } from './hexRuns.js'

/** A generation's key prefix in the deployment's `INDEX_R2` bucket. */
export const staticPrefix = (gen: string): string => `static-names/${gen}`
/** The deployment's generation, its `STATIC_GEN` var (e.g. gcs's `2026-10-08c`, cw's `2026-10-09cw`): required with the
 *  static index on, one path segment. */
export function staticGen(env: { STATIC_GEN?: string }): string {
  const gen = env.STATIC_GEN?.trim()
  if (!gen) throw new Error('static names: STATIC_GEN is unset (the static name index generation in INDEX_R2)')
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(gen)) throw new Error(`static names: bad STATIC_GEN ${JSON.stringify(gen)}`)
  return gen
}

// --- order --------------------------------------------------------------------------------------

/** A UTF-16 code unit remapped so that comparing units compares code points (surrogates above the BMP). */
const unit = (c: number): number => c < 0xd800 ? c : c < 0xe000 ? c + 0x2000 : c - 0x800
/** Code-point order (DuckDB's and Python's string order), which plain `<` breaks only around surrogates. */
export function cmp(a: string, b: string): number {
  const n = Math.min(a.length, b.length)
  for (let i = 0; i < n; i++) {
    const x = a.charCodeAt(i), y = b.charCodeAt(i)
    if (x !== y) return unit(x) - unit(y)
  }
  return a.length - b.length
}
/** The first index in `[0, n)` whose `pred` holds (`pred` monotone). */
function bisect(n: number, pred: (i: number) => boolean): number {
  let lo = 0, hi = n
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (pred(mid)) hi = mid; else lo = mid + 1 }
  return lo
}

// --- shard plan ---------------------------------------------------------------------------------

export interface Shard { i: number; lo: string; hi: string | null; rows: number }
/** The shard whose `[lo, hi)` holds the key's first three characters, or null (no suffix that low). */
export function shardFor(shards: Shard[], key: string): Shard | null {
  const p3 = [...key].slice(0, 3).join('')
  const k = bisect(shards.length, i => cmp(shards[i].lo, p3) > 0) - 1
  if (k < 0) return null
  const s = shards[k]
  return s.hi == null || cmp(p3, s.hi) < 0 ? s : null
}
export const shardFile = (i: number): string => `sx/s${String(i).padStart(4, '0')}.parquet`

// --- footer → group index -----------------------------------------------------------------------

/** The sx schema's leaves, in file order (checked against each footer). `vf`/`vt` are left as their
 *  physical INT64 (epoch ms as bigint): no timestamp conversion. */
export const SX_COLUMNS = ['s', 'depth', 'path', 'usr', 'vf', 'vt', 'size', 'n_files'] as const
const SX_TYPES = ['BYTE_ARRAY', 'INT32', 'BYTE_ARRAY', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64', 'INT64']
/** A flat file of REQUIRED columns (zstd), as hyparquet needs it to decode one row group without a footer. */
export interface FlatSchema { columns: readonly string[]; types: readonly string[] }
const schemaOf = ({ columns, types }: FlatSchema) => [
  { name: 'schema', repetition_type: 'REQUIRED', num_children: columns.length },
  ...columns.map((name, i) => ({ name, type: types[i], repetition_type: 'REQUIRED', ...(types[i] === 'BYTE_ARRAY' ? { converted_type: 'UTF8' } : {}) })),
]
export const SX: FlatSchema = { columns: SX_COLUMNS, types: SX_TYPES }
const NC = SX_COLUMNS.length
const ZSTD = 6

/** One shard's row groups: `s` min/max (exact: the writer's statistics are untruncated, `is_*_exact`),
 *  rows, and per column chunk `data_page_offset, total_compressed_size, dictionary_page_offset | 0` — the
 *  only column metadata hyparquet reads — flat: column `c` of group `g` at `chunks[(g * NC + c) * 3]`. */
export interface GroupIndex { size: number; sMin: string[]; sMax: string[]; rows: number[]; chunks: number[] }

/** A minimal Thrift compact-protocol walker over a parquet footer: only the fields the index needs are
 *  decoded; everything else (schema, encodings, page statistics, size statistics, kv metadata) is skipped. */
class Compact {
  pos = 0
  constructor(readonly b: Uint8Array) {}
  varint(): number {
    let x = 0, mul = 1, byte
    do { byte = this.b[this.pos++]; x += (byte & 0x7f) * mul; mul *= 128 } while (byte & 0x80)
    return x
  }
  zigzag(): number { const v = this.varint(); return v % 2 ? -(v + 1) / 2 : v / 2 }
  binary(): Uint8Array { const n = this.varint(); const out = this.b.subarray(this.pos, this.pos + n); this.pos += n; return out }
  /** Iterate a struct's fields; `on(fid, type)` returns true when it consumed the value. */
  struct(on: (fid: number, type: number) => boolean): void {
    let fid = 0
    for (;;) {
      const byte = this.b[this.pos++], type = byte & 0x0f
      if (type === 0) return
      const delta = byte >> 4
      fid = delta ? fid + delta : this.zigzag()
      if (!on(fid, type)) this.skip(type)
    }
  }
  list(each: (type: number, i: number) => void): void {
    const byte = this.b[this.pos++]
    let n = byte >> 4
    if (n === 15) n = this.varint()
    const type = byte & 0x0f
    for (let i = 0; i < n; i++) each(type, i)
  }
  skip(type: number): void {
    switch (type) {
      case 1: case 2: return
      case 3: this.pos++; return
      case 4: case 5: case 6: this.varint(); return
      case 7: this.pos += 8; return
      case 8: { const n = this.varint(); this.pos += n; return }
      case 9: case 10: this.list(t => (t === 1 || t === 2) ? this.pos++ : this.skip(t)); return
      case 11: {
        const n = this.varint()
        if (!n) return
        const kv = this.b[this.pos++]
        for (let i = 0; i < n; i++) { this.skip(kv >> 4); this.skip(kv & 0x0f) }
        return
      }
      case 12: this.struct(() => false); return
      default: throw new Error(`thrift: unknown compact type ${type}`)
    }
  }
}

const utf8 = new TextDecoder()

/** Cut a shard's group index from its footer (the FileMetaData thrift, without the trailing length/magic). */
export function groupIndex(footer: Uint8Array, size: number): GroupIndex {
  const r = new Compact(footer)
  const idx: GroupIndex = { size, sMin: [], sMax: [], rows: [], chunks: [] }
  r.struct((fid, type) => {
    if (fid !== 4 || type !== 9) return false
    r.list(() => {
      let nCols = 0
      r.struct((rf, rt) => {
        if (rf === 3 && rt === 6) { idx.rows.push(r.varint() / 2); return true } // num_rows: zigzag of a non-negative i64
        if (rf !== 1 || rt !== 9) return false
        r.list((_, c) => {
          nCols++
          let dpo = 0, total = 0, dict = 0, codec = -1, min: string | null = null, max: string | null = null
          r.struct((cf, ct) => {
            if (cf !== 3 || ct !== 12) return false
            r.struct((mf, mt) => {
              if (mf === 4) { codec = r.zigzag(); return true }
              if (mf === 7) { total = r.zigzag(); return true }
              if (mf === 9) { dpo = r.zigzag(); return true }
              if (mf === 11) { dict = r.zigzag(); return true }
              if (mf === 12 && c === 0) {
                r.struct((sf, st) => {
                  if (sf === 5 && st === 8) { max = utf8.decode(r.binary()); return true }
                  if (sf === 6 && st === 8) { min = utf8.decode(r.binary()); return true }
                  return false
                })
                return true
              }
              return false
            })
            return true
          })
          if (codec !== ZSTD) throw new Error(`sx footer: column ${c} codec ${codec}, expected ZSTD`)
          if (c === 0) {
            if (min == null || max == null) throw new Error('sx footer: a row group has no exact `s` statistics')
            idx.sMin.push(min); idx.sMax.push(max)
          }
          idx.chunks.push(dpo, total, dict)
        })
        if (nCols !== NC) throw new Error(`sx footer: ${nCols} columns, expected ${NC}`)
        return true
      })
    })
    return true
  })
  if (idx.rows.length !== idx.sMin.length) throw new Error('sx footer: row group count mismatch')
  return idx
}

/** The groups `[a, b)` whose `s` range can hold suffixes starting with `key` (`Reader.rows`' bisects); `exact`:
 *  only `s == key` (`b` = the first group whose `s_min` is past it). */
export function selectGroups(idx: GroupIndex, key: string, exact = false): [number, number] {
  const n = idx.sMin.length, top = key + '\u{10FFFF}'
  const a = bisect(n, i => cmp(idx.sMax[i], key) >= 0)
  const b = exact ? bisect(n, i => cmp(idx.sMin[i], key) > 0) : bisect(n, i => cmp(idx.sMin[i], top) >= 0)
  return [a, Math.max(a, b)]
}

/** A shard's per-group `path` bounds (specs/search-extensions.md §2, "path keys"): the `path` column chunk's
 *  min/max statistics (untruncated: the writer's, as `s`'s), `null` where a group has none. Within a group the
 *  rows of one `s` are sorted by `path`, so the rows of `s == q` in group `g` lie in `[pMin[g], pMax[g]]`: with
 *  `selectGroups(…, exact)` they bound `(q, path)` key ranges by whole groups, the suffix shards and the name
 *  index read as roots files (`q$`, `^q$` under a view path) with no file of keys built. */
export interface PathKeys { pMin: (string | null)[]; pMax: (string | null)[] }

/** Cut a shard's `PathKeys` from its footer (as `groupIndex`, the `path` column's statistics). */
export function pathKeys(footer: Uint8Array): PathKeys {
  const r = new Compact(footer)
  const keys: PathKeys = { pMin: [], pMax: [] }
  r.struct((fid, type) => {
    if (fid !== 4 || type !== 9) return false
    r.list(() => {
      r.struct((rf, rt) => {
        if (rf !== 1 || rt !== 9) return false
        r.list((_, c) => {
          let min: string | null = null, max: string | null = null
          r.struct((cf, ct) => {
            if (cf !== 3 || ct !== 12) return false
            r.struct((mf) => {
              if (mf !== 12 || c !== PATH_COL) return false
              r.struct((sf, st) => {
                if (sf === 5 && st === 8) { max = utf8.decode(r.binary()); return true }
                if (sf === 6 && st === 8) { min = utf8.decode(r.binary()); return true }
                return false
              })
              return true
            })
            return true
          })
          if (c === PATH_COL) { keys.pMin.push(min); keys.pMax.push(max) }
        })
        return true
      })
    })
    return true
  })
  return keys
}
const PATH_COL = SX_COLUMNS.indexOf('path')

/** Groups of `[a, b)` that can hold a row with `path` in `[lo, hi)` (by `keys`; a group without bounds is kept). */
export function groupsUnder(keys: PathKeys, a: number, b: number, lo: string, hi: string): number[] {
  const out: number[] = []
  for (let g = a; g < b; g++) {
    const mn = keys.pMin[g], mx = keys.pMax[g]
    if (mn != null && mx != null && (cmp(mx, lo) < 0 || cmp(mn, hi) >= 0)) continue
    out.push(g)
  }
  return out
}

/** The key range `[lo, hi)` of the paths strictly under `P` (`''`: every path). */
export const underRange = (P: string): [string, string] => P === '' ? ['', '\u{10FFFF}'] : [P + '/', P + '0']

/** The byte span `[start, end)` of groups `[a, b)` (their column chunks are contiguous). */
export function groupSpan(idx: GroupIndex, a: number, b: number): [number, number] {
  let start = Infinity, end = 0
  for (let g = a; g < b; g++) for (let c = 0; c < NC; c++) {
    const k = (g * NC + c) * 3, dpo = idx.chunks[k], total = idx.chunks[k + 1], dict = idx.chunks[k + 2]
    const s = dict > 0 ? Math.min(dict, dpo) : dpo
    start = Math.min(start, s); end = Math.max(end, s + total)
  }
  return [start, end]
}

// --- decode + answer ----------------------------------------------------------------------------

export interface SxColumns { s: string[]; depth: number[]; path: string[]; usr: string[]; vf: bigint[]; vt: bigint[]; size: bigint[]; n_files: bigint[] }

/** Decode one row group from fetched bytes (`buf` holds the file's `[start, start + buf.byteLength)`), given
 *  its rows and per column `data_page_offset, total_compressed_size, dictionary_page_offset | 0` (`chunks`,
 *  file order): a FileMetaData is built from those alone, so no footer is read. */
export async function decodeFlat(spec: FlatSchema, size: number, rows: number, chunks: ArrayLike<number>, buf: ArrayBuffer, start: number): Promise<Record<string, unknown[]>> {
  const group = {
    num_rows: BigInt(rows),
    columns: spec.columns.map((name, c) => {
      const dict = chunks[c * 3 + 2]
      return { meta_data: { type: spec.types[c], path_in_schema: [name], codec: 'ZSTD', data_page_offset: BigInt(chunks[c * 3]), total_compressed_size: BigInt(chunks[c * 3 + 1]), ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}) } }
    }),
  } as unknown as RowGroup
  const metadata = { version: 1, schema: schemaOf(spec), num_rows: BigInt(rows), row_groups: [group], metadata_length: 0 } as unknown as FileMetaData
  const file = {
    byteLength: size,
    slice(s: number, e?: number) {
      const end = e ?? size
      if (s < start || end > start + buf.byteLength) throw new Error(`static names: read outside the fetched span: ${s}-${end}`)
      return buf.slice(s - start, end - start)
    },
  }
  const out = Object.fromEntries(spec.columns.map(c => [c, new Array(rows)])) as Record<string, unknown[]>
  await parquetRead({
    file, metadata, columns: [...spec.columns], compressors,
    onChunk: ({ columnName, columnData, rowStart }) => {
      const arr = out[columnName]
      for (let i = 0; i < columnData.length; i++) arr[rowStart + i] = columnData[i]
    },
  })
  return out
}

/** Decode one shard row group `g` from the fetched bytes. */
export async function decodeGroup(idx: GroupIndex, g: number, buf: ArrayBuffer, start: number): Promise<SxColumns> {
  return await decodeFlat(SX, idx.size, idx.rows[g], idx.chunks.slice(g * NC * 3, (g + 1) * NC * 3), buf, start) as unknown as SxColumns
}

/** A scan id's instant (epoch ms, `scanSlug.ts` `scanTime`; `scan_id.scan_epoch` × 1000), refusing anything but a
 *  full scan id: a version is live on a scan by comparing stamps, so a bad id must not read as "never". */
export function scanAt(id: string): number {
  if (!isScanId(id)) throw new Error(`bad scan id ${id}`)
  return scanTime(id)
}

export type Totals = Record<string, [bigint, bigint]>
export interface Answer { rows_read: number; rows_matching: number; answers: Record<string, Totals> }

/** One first hit: a version of a `(path, usr)` slice whose lowercase name contains the key and whose
 *  lowercase parent does not (depth ≥ 1, deduped by `(path, usr, vf)`), live on `[vf, vt)` (epoch ms). Only
 *  liveness is left to decide per date. */
export interface Hit { path: string; depth: number; usr: string; vf: number; vt: number; size: bigint; n: bigint }

/** The first-hit fold (`Reader.answer`), fed one decoded row group at a time so only first hits are held.
 *  Everything but liveness is decided once per row: the suffix and name match, depth, the `(path, usr, vf)`
 *  dedup (a version's rows whose suffix starts with the key are its occurrences of the key, so only a name
 *  holding it twice or more is remembered) and the parent test; each date then checks `vf ≤ D < vt` over
 *  the first hits. */
export class FirstHits {
  rows_read = 0
  rows_matching = 0
  readonly hits: Hit[] = []
  private seen = new Set<string>()
  /** `rule`: the generation's hex-run rule (`hexRuns.ts`; null = the full index): "contains" is `occurs` under it. */
  constructor(readonly key: string, readonly rule: HexRule | null = null) {}
  add(cols: SxColumns): void {
    const key = this.key, rule = this.rule
    this.rows_read += cols.path.length
    for (let i = 0; i < cols.path.length; i++) {
      if (!cols.s[i].startsWith(key)) continue
      const p = cols.path[i], slash = p.lastIndexOf('/'), name = p.slice(slash + 1).toLowerCase()
      const at = firstOccurrence(key, name, rule)
      if (at < 0) continue
      this.rows_matching++
      if (cols.depth[i] < 1) continue
      if (firstOccurrence(key, name, rule, at + 1) >= 0) {
        const k = `${p}\0${cols.usr[i]}\0${cols.vf[i]}`
        if (this.seen.has(k)) continue
        this.seen.add(k)
      }
      if (occurs(key, (slash < 0 ? '' : p.slice(0, slash)).toLowerCase(), rule)) continue
      this.hits.push({ path: p, depth: cols.depth[i], usr: cols.usr[i], vf: Number(cols.vf[i]), vt: Number(cols.vt[i]), size: cols.size[i], n: cols.n_files[i] })
    }
  }
  answer(dates: string[]): Answer {
    const answers: Record<string, Totals> = {}
    for (const d of dates) {
      const D = scanAt(d), sums = new Map<string, [bigint, bigint]>()
      for (const h of this.hits) {
        if (!(h.vf <= D && D < h.vt)) continue
        const first = h.path.indexOf('/'), bucket = first < 0 ? h.path : h.path.slice(0, first)
        const t = sums.get(bucket)
        if (t) { t[0] += h.size; t[1] += h.n } else sums.set(bucket, [h.size, h.n])
      }
      answers[d] = Object.fromEntries([...sums].sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0))
    }
    return { rows_read: this.rows_read, rows_matching: this.rows_matching, answers }
  }
}

// --- storage seam (R2 in the Worker, files in tests) -------------------------------------------

/** Ranged reads of the generation's objects (keys relative to a generation's `staticPrefix`). */
export interface Blobs {
  /** `[offset, offset + length)` of `key`; `length` omitted = to the end. */
  range(key: string, offset: number, length?: number): Promise<ArrayBuffer>
  /** The last `n` bytes and the object's size. */
  suffix(key: string, n: number): Promise<{ buf: ArrayBuffer; size: number }>
  json<T>(key: string): Promise<T>
  /** Keys under `prefix` (relative to the same root), when the store can list (the runs' manifests). */
  list?(prefix: string): Promise<string[]>
}

/** Where a query's time and bytes went (the `x-static-io` header). */
export interface Io { shard: number | null; groups: number; bytes: number; rows_read: number; rows_matching: number; index: 'isolate' | 'cache' | 'footer' | 'none'; footer_bytes?: number; ms: Record<string, number>; tiers?: number }

export interface IndexCache<T = GroupIndex> { get(file: string): Promise<T | null>; put(file: string, idx: T): Promise<void> }

/** How `StaticNames.read` selects and folds a key's rows: `exact` — the rows with `s == key` (else every `s`
 *  starting with it); `under` — only the groups whose path keys meet the paths under it (rows outside are dropped by
 *  `fold`); `fold` — the first-hit rule (default `FirstHits`, the contains literal's). */
export interface ReadOpts { exact?: boolean; under?: string; fold?: (key: string) => FirstHits; rule?: HexRule | null }

export class StaticNames {
  private shards?: Promise<Shard[]>
  private indexes = new Map<string, Promise<GroupIndex>>()
  private pathKeyMap = new Map<string, Promise<PathKeys>>()
  constructor(readonly blobs: Blobs, readonly cache?: IndexCache, readonly clock: () => Promise<number> = async () => Date.now(), readonly maxIndexes = 8,
    readonly keysCache?: IndexCache<PathKeys>) {}

  plan(): Promise<Shard[]> {
    this.shards ??= this.blobs.json<{ shards: Shard[] }>('shards.json').then(d => d.shards).catch(e => { this.shards = undefined; throw e })
    return this.shards
  }

  /** A shard's group index: the isolate's, else the colo cache's, else cut from the footer (one suffix GET
   *  sized from the shard's row count, a second only if the footer is bigger than guessed). */
  private async index(shard: Shard, io: Io): Promise<GroupIndex> {
    const file = shardFile(shard.i)
    const held = this.indexes.get(file)
    if (held) { this.indexes.delete(file); this.indexes.set(file, held); io.index = 'isolate'; return held }
    const p = (async () => {
      const cached = await this.cache?.get(file)
      if (cached) { io.index = 'cache'; return cached }
      io.index = 'footer'
      const { footer, size } = await this.footer(shard, io)
      const idx = groupIndex(footer, size)
      await this.cache?.put(file, idx)
      return idx
    })()
    this.indexes.set(file, p)
    p.catch(() => this.indexes.delete(file))
    while (this.indexes.size > this.maxIndexes) this.indexes.delete(this.indexes.keys().next().value!)
    return p
  }

  /** A shard's footer (the FileMetaData thrift): one suffix GET sized from its row count, a second only if the footer
   *  is bigger than guessed. */
  private async footer(shard: Shard, io: Io): Promise<{ footer: Uint8Array; size: number }> {
    const file = shardFile(shard.i)
    const groups = Math.ceil(shard.rows / 8192)
    let { buf, size } = await this.blobs.suffix(file, Math.min(groups * 1600 + (64 << 10), 64 << 20))
    const flen = new DataView(buf, buf.byteLength - 8).getUint32(0, true)
    if (flen + 8 > buf.byteLength) buf = (await this.blobs.suffix(file, flen + 8)).buf
    io.footer_bytes = (io.footer_bytes ?? 0) + flen
    return { footer: new Uint8Array(buf, buf.byteLength - 8 - flen, flen), size }
  }

  /** A shard's path keys (`pathKeys`): the isolate's, else the colo cache's, else cut from the footer. Only the
   *  anchored readers' scoped reads need them, so they are kept apart from the group index. */
  private async keys(shard: Shard, io: Io): Promise<PathKeys> {
    const file = shardFile(shard.i)
    const held = this.pathKeyMap.get(file)
    if (held) { this.pathKeyMap.delete(file); this.pathKeyMap.set(file, held); return held }
    const p = (async () => {
      const cached = await this.keysCache?.get(file)
      if (cached) return cached
      const keys = pathKeys((await this.footer(shard, io)).footer)
      await this.keysCache?.put(file, keys)
      return keys
    })()
    this.pathKeyMap.set(file, p)
    p.catch(() => this.pathKeyMap.delete(file))
    while (this.pathKeyMap.size > this.maxIndexes) this.pathKeyMap.delete(this.pathKeyMap.keys().next().value!)
    return p
  }

  /** The groups a read of `key` touches (`ReadOpts`: `exact`, `under`) and the rows they hold — an upper bound on the
   *  matching rows, before any data is read. */
  async select(key: string, io: Io, opts: ReadOpts = {}): Promise<{ shard: Shard; idx: GroupIndex; groups: number[]; rows: number } | null> {
    const shard = shardFor(await this.plan(), key)
    io.shard = shard?.i ?? null
    if (!shard) return null
    const idx = await this.index(shard, io)
    const [a, b] = selectGroups(idx, key, opts.exact)
    let groups: number[]
    if (opts.under == null || opts.under === '' || a === b) groups = Array.from({ length: b - a }, (_, k) => a + k)
    else groups = groupsUnder(await this.keys(shard, io), a, b, ...underRange(opts.under))
    let rows = 0
    for (const g of groups) rows += idx.rows[g]
    return { shard, idx, groups, rows }
  }

  /** How many suffix rows (row-group granular, an upper bound on the matching rows) `key`'s range holds:
   *  the dispatch's bound, before any data is read. */
  async extent(key: string, io: Io): Promise<{ shard: Shard; idx: GroupIndex; a: number; b: number; rows: number } | null> {
    const shard = shardFor(await this.plan(), key)
    io.shard = shard?.i ?? null
    if (!shard) return null
    const idx = await this.index(shard, io)
    const [a, b] = selectGroups(idx, key)
    let rows = 0
    for (let g = a; g < b; g++) rows += idx.rows[g]
    return { shard, idx, a, b, rows }
  }

  /** `key`'s first hits (lowercased, ≥ 3 characters): its range read once and folded; `maxRows` refuses
   *  (null) a range above it before any data is fetched. `opts`: the anchored readers' selection and fold, and
   *  the generation's hex-run rule (`rule`, the default fold's). */
  async read(key: string, maxRows = Infinity, opts: ReadOpts = {}): Promise<{ io: Io; fold: FirstHits | null }> {
    const io: Io = { shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }
    let t = await this.clock()
    const lap = async (name: string) => { const now = await this.clock(); io.ms[name] = now - t; t = now }
    const sel = await this.select(key, io, opts)
    await lap('index')
    const fold = opts.fold ? opts.fold(key) : new FirstHits(key, opts.rule ?? null)
    if (!sel || !sel.groups.length) return { io, fold }
    io.groups = sel.groups.length
    if (sel.rows > maxRows) { io.rows_read = sel.rows; return { io, fold: null } }
    // Contiguous runs of the selected groups, one ranged GET each (a contains range is one run).
    const runs: [number, number][] = []
    for (const g of sel.groups) { const last = runs[runs.length - 1]; if (last && last[1] === g) last[1] = g + 1; else runs.push([g, g + 1]) }
    const bufs = await Promise.all(runs.map(async ([a, b]) => {
      const [start, end] = groupSpan(sel.idx, a, b)
      return { a, b, start, buf: await this.blobs.range(shardFile(sel.shard.i), start, end - start) }
    }))
    io.bytes = bufs.reduce((n, x) => n + x.buf.byteLength, 0)
    await lap('fetch')
    // One group at a time: peak memory is one group's columns plus the first hits, not the whole range.
    let decode = 0
    for (const { a, b, start, buf } of bufs) for (let g = a; g < b; g++) {
      const t1 = await this.clock()
      const cols = await decodeGroup(sel.idx, g, buf, start)
      const t2 = await this.clock()
      decode += t2 - t1
      fold.add(cols)
    }
    await lap('decode')
    io.ms.filter = io.ms.decode - decode
    io.ms.decode = decode
    io.rows_read = fold.rows_read
    io.rows_matching = fold.rows_matching
    return { io, fold }
  }

  /** Answer `key` (lowercased, ≥ 3 characters) on each date; `maxRows` refuses (null) a range above it
   *  before any data is fetched. */
  async answer(key: string, dates: string[], maxRows = Infinity, rule: HexRule | null = null): Promise<{ io: Io; answer: Answer | null }> {
    const { io, fold } = await this.read(key, maxRows, { rule })
    if (!fold) return { io, answer: null }
    const t = await this.clock()
    const answer = fold.answer(dates)
    io.ms.answer = await this.clock() - t
    return { io, answer }
  }
}

/** `Blobs` over an R2 bucket binding (keys under `prefix`, a generation's `staticPrefix`). */
export function r2Blobs(r2: R2Bucket, prefix: string): Blobs {
  const get = async (key: string, range?: R2Range) => {
    const o = await r2.get(`${prefix}/${key}`, range ? { range } : undefined)
    if (!o) throw new Error(`static names: ${prefix}/${key} is missing`)
    return o
  }
  return {
    range: async (key, offset, length) => (await get(key, length == null ? { offset } : { offset, length })).arrayBuffer(),
    suffix: async (key, n) => { const o = await get(key, { suffix: n }); return { buf: await o.arrayBuffer(), size: o.size } },
    json: async key => (await get(key)).json(),
    list: async p => {
      const keys: string[] = []
      let cursor: string | undefined
      do {
        const page = await r2.list({ prefix: `${prefix}/${p}`, cursor })
        for (const o of page.objects) keys.push(o.key.slice(prefix.length + 1))
        cursor = page.truncated ? page.cursor : undefined
      } while (cursor)
      return keys
    },
  }
}

/** The colo's Cache API as the index tier between isolates (JSON; ~1 MB per shard group index). */
export function cacheIndexes<T = GroupIndex>(cache: Cache, prefix: string, version = 'index-v2'): IndexCache<T> {
  const url = (file: string) => `https://static-names.invalid/${prefix}/${file}.${version}.json`
  return {
    async get(file) {
      const r = await cache.match(url(file))
      return r ? r.json() as Promise<T> : null
    },
    async put(file, idx) {
      await cache.put(url(file), new Response(JSON.stringify(idx), { headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=604800' } }))
    },
  }
}
