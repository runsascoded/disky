/** The static name catalog (specs/architecture/static-name-search.md, "Static catalog"): precomputed
 *  per-bucket first-hit answers for exactly the literals the suffix reader (`staticNames.ts`) should not
 *  read itself, so every `/names` literal on every scan of the generation is answered from R2 alone.
 *
 *  Membership (`static_catalog.py`): a lowercase literal is a member iff it has one or two characters and
 *  occurs in some name, or its suffix range holds more than V (`meta.json` `membership.max_rows`) rows. So
 *  a one- or two-character miss is zero everywhere, and a longer miss reads at most V rows statically.
 *
 *  Layout, under `static-names/<gen>/catalog/`: `cells.parquet` — `(q, bucket, vf, b, o)` sorted `(q,
 *  bucket, vf)` in code-point order, 4,096-row groups, each member led by a header row `(q, '', 0, rows,
 *  n)`, the rest running totals per bucket at each change time (`vf`, epoch seconds); `index.parquet` — per
 *  row group its exact first and last `q`, byte span, rows, and per column `data_page_offset,
 *  total_compressed_size, dictionary_page_offset | 0`, so a group decodes without the footer;
 *  `meta.json` — the generation, V, and `cells.parquet`'s size.
 *
 *  A lookup (`Catalog.rows`): groups `[a, b)` with `a` = first whose `q_max ≥ q`, `b` = first whose
 *  `q_min > q`; none → not a member. Else ONE ranged GET of their bytes, decoded group by group, keeping
 *  the rows whose `q` equals the literal; the first must be the header. A bucket's value on date `D` is its
 *  newest cell with `vf ≤ D` (none: zero). */
import { parquetReadObjects } from 'hyparquet'
import { type Blobs, cmp, decodeFlat, type FlatSchema, type IndexCache, scanAt, type Totals } from './staticNames.js'
import { compressors } from './zstd.js'
import { ruleOf } from './hexRuns.js'

export const CELLS = 'catalog/cells.parquet'
export const CELL_SCHEMA: FlatSchema = { columns: ['q', 'bucket', 'vf', 'b', 'o'], types: ['BYTE_ARRAY', 'BYTE_ARRAY', 'INT64', 'INT64', 'INT64'] }
const NC = CELL_SCHEMA.columns.length

/** `hex_runs`: the generation's hex-run rule (`hexRuns.ts`), recorded by every tier; absent = the full index. */
export interface CatalogMeta { gen: string; bytes: number; cells_rows: number; row_groups: number; membership: { max_rows: number }; hex_runs?: { min: number; tail: number } }
/** `index.parquet`, column-wise; `chunks` flat: column `c` of group `g` at `(g * 5 + c) * 3`. */
export interface CatalogIndex { size: number; qMin: string[]; qMax: string[]; offset: number[]; length: number[]; rows: number[]; chunks: number[] }
export interface Cell { bucket: string; vf: bigint; b: bigint; o: bigint }
/** A member: its suffix-range rows (−1 for a one- or two-character literal) and its cells, header excluded. */
export interface Member { q: string; rows: number; cells: Cell[]; n?: number }
export interface CatalogIo { groups: number; bytes: number; rows_read: number; index: 'isolate' | 'cache' | 'file' }

const num = (v: unknown): number => { const n = Number(v); if (!Number.isSafeInteger(n)) throw new Error(`static catalog: bad integer ${v}`); return n }

/** Decode `index.parquet` (59 KB; read whole) into the lookup's arrays, checking it covers `meta`'s file. */
export async function catalogIndex(buf: ArrayBuffer, meta: CatalogMeta): Promise<CatalogIndex> {
  const rows = await parquetReadObjects({ file: buf, compressors }) as { rg: number; q_min: string; q_max: string; offset: bigint; length: bigint; rows: number; chunks: bigint[] }[]
  rows.sort((x, y) => x.rg - y.rg)
  if (rows.length !== meta.row_groups || rows.some((r, i) => r.rg !== i || r.chunks.length !== NC * 3)) throw new Error('static catalog: index.parquet does not match meta.json')
  const idx: CatalogIndex = { size: meta.bytes, qMin: [], qMax: [], offset: [], length: [], rows: [], chunks: [] }
  for (const r of rows) {
    idx.qMin.push(r.q_min); idx.qMax.push(r.q_max); idx.offset.push(num(r.offset)); idx.length.push(num(r.length)); idx.rows.push(num(r.rows))
    for (const c of r.chunks) idx.chunks.push(num(c))
  }
  if (idx.rows.reduce((a, b) => a + b, 0) !== meta.cells_rows) throw new Error('static catalog: index rows do not sum to meta.json cells_rows')
  return idx
}

/** The row groups `[a, b)` that can hold `q` (`a === b`: not a member). */
export function catalogGroups(idx: CatalogIndex, q: string): [number, number] {
  const bisect = (pred: (i: number) => boolean) => { let lo = 0, hi = idx.qMin.length; while (lo < hi) { const m = (lo + hi) >>> 1; if (pred(m)) hi = m; else lo = m + 1 } return lo }
  const a = bisect(i => cmp(idx.qMax[i], q) >= 0), b = bisect(i => cmp(idx.qMin[i], q) > 0)
  return [a, Math.max(a, b)]
}

/** Per date, each bucket's newest cell at or before it (zeros omitted): `Catalog.answer`. */
export function catalogAnswer(member: Member, dates: string[]): Record<string, Totals> {
  const out: Record<string, Totals> = {}
  for (const d of dates) {
    const D = BigInt(scanAt(d) / 1000), cur = new Map<string, [bigint, bigint]>()
    for (const c of member.cells) if (c.vf <= D) cur.set(c.bucket, [c.b, c.o])
    out[d] = Object.fromEntries([...cur].filter(([, [b, o]]) => b !== 0n || o !== 0n).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0))
  }
  return out
}

export class StaticCatalog {
  private meta?: Promise<CatalogMeta>
  private idx?: Promise<CatalogIndex>
  constructor(readonly blobs: Blobs, readonly cache?: IndexCache<CatalogIndex>) {}

  /** `meta.json` (isolate-held). */
  info(): Promise<CatalogMeta> {
    this.meta ??= this.blobs.json<CatalogMeta>('catalog/meta.json').then(m => {
      if (!Number.isSafeInteger(m.membership?.max_rows) || m.membership.max_rows < 1 || !Number.isSafeInteger(m.bytes)) throw new Error('static catalog: bad meta.json')
      ruleOf(m.hex_runs)
      return m
    })
    this.meta.catch(() => { this.meta = undefined })
    return this.meta
  }

  /** The index: the isolate's, else the colo cache's, else `index.parquet` decoded (and cached). */
  private async index(io: CatalogIo): Promise<CatalogIndex> {
    if (this.idx) { io.index = 'isolate'; return this.idx }
    const p = (async () => {
      const cached = await this.cache?.get('catalog/index.parquet')
      if (cached) { io.index = 'cache'; return cached }
      io.index = 'file'
      const [meta, buf] = await Promise.all([this.info(), this.blobs.range('catalog/index.parquet', 0)])
      const idx = await catalogIndex(buf, meta)
      await this.cache?.put('catalog/index.parquet', idx)
      return idx
    })()
    this.idx = p
    p.catch(() => { if (this.idx === p) this.idx = undefined })
    return p
  }

  /** `q`'s cells, or null when it is not a member. `partial`: this file is one tier of several (the base and its runs,
   *  `staticRuns.ts`): its cells are a part, and the header's count (`n`) covers every tier, so it is returned,
   *  not checked here. */
  async lookup(q: string, opts: { partial?: boolean } = {}): Promise<{ io: CatalogIo; member: Member | null }> {
    const io: CatalogIo = { groups: 0, bytes: 0, rows_read: 0, index: 'isolate' }
    const idx = await this.index(io)
    const [a, b] = catalogGroups(idx, q)
    if (a === b) return { io, member: null }
    io.groups = b - a
    const start = idx.offset[a], end = idx.offset[b - 1] + idx.length[b - 1]
    const buf = await this.blobs.range(CELLS, start, end - start)
    io.bytes = buf.byteLength
    const mine: Cell[] = []
    let head: Cell | undefined
    for (let g = a; g < b; g++) {
      const cols = await decodeFlat(CELL_SCHEMA, idx.size, idx.rows[g], idx.chunks.slice(g * NC * 3, (g + 1) * NC * 3), buf, start) as { q: string[]; bucket: string[]; vf: bigint[]; b: bigint[]; o: bigint[] }
      io.rows_read += cols.q.length
      for (let i = 0; i < cols.q.length; i++) {
        if (cols.q[i] !== q) continue
        const cell = { bucket: cols.bucket[i], vf: cols.vf[i], b: cols.b[i], o: cols.o[i] }
        if (!head) { head = cell; continue }
        mine.push(cell)
      }
    }
    if (!head || head.bucket !== '') return { io, member: null }
    if (opts.partial) return { io, member: { q, rows: num(head.b), cells: mine, n: num(head.o) } }
    if (BigInt(mine.length) !== head.o) throw new Error(`static catalog: ${JSON.stringify(q)} header counts ${head.o} cells, found ${mine.length}`)
    return { io, member: { q, rows: num(head.b), cells: mine } }
  }
}
