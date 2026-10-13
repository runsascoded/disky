/** Dev-only bench (`STATIC_BENCH=1`): time R2 ranged reads, and hyparquet decode, of the static name-search
 *  prototype (`proto/static/*.parquet` in `INDEX_R2`) from Pages Functions. Timings and counts only, never rows.
 *  POST `{ file, ranges: [{ q, off, len }], mode: 'seq' | 'par', decode }`.
 *
 *  GET `?q=<literal>&d=<scan>[&d=<scan>…]`: the static reader (`_lib/staticNames.ts`, the built generation)
 *  run directly, with no row budget and no catalog dispatch — its per-date `{bucket: [bytes, objects]}` (the
 *  shape `dt-cloud static-names query` prints) and its I/O and phase timings, for verification. */
import { type FileMetaData, parquetMetadata, parquetReadObjects } from 'hyparquet'
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { names } from '../_lib/nameSummaryStatic.js'
import { staticGen } from '../_lib/staticNames.js'
import { compressors } from '../_lib/zstd.js'
import { isScanId } from '../../src/scanSlug.js'

type Env = { INDEX_R2?: R2Bucket, STATIC_BENCH?: string, STATIC_GEN?: string }
type Range = { q: string, off: number, len: number }
type Meta = { size: number, footOff: number, foot: ArrayBuffer, md: FileMetaData, fetch_ms: number, parse_ms: number }

const metas = new Map<string, Meta>()
// The Workers clock only advances across I/O: a cache miss pins "now" after CPU-bound work.
const tick = async () => { await caches.default.match('https://bench.invalid/x'); return Date.now() }

async function meta(r2: R2Bucket, file: string): Promise<Meta & { cached: boolean }> {
  const hit = metas.get(file)
  if (hit) return { ...hit, cached: true }
  const key = `proto/static/${file}`
  const t0 = Date.now()
  const size = (await r2.head(key))!.size
  const tail = await (await r2.get(key, { range: { offset: size - 8, length: 8 } }))!.arrayBuffer()
  const flen = new DataView(tail).getUint32(0, true)
  const footOff = size - 8 - flen
  const foot = await (await r2.get(key, { range: { offset: footOff, length: flen + 8 } }))!.arrayBuffer()
  const t1 = Date.now()
  const md = parquetMetadata(foot)
  const t2 = await tick()
  const m = { size, footOff, foot, md, fetch_ms: t1 - t0, parse_ms: t2 - t1 }
  metas.set(file, m)
  return { ...m, cached: false }
}

function rowSpan(md: FileMetaData, off: number, len: number) {
  let row = 0, a = -1, b = -1, start = 0
  md.row_groups.forEach((rg, i) => {
    const c = rg.columns[0].meta_data!
    const o = Number(c.dictionary_page_offset ?? c.data_page_offset)
    if (o >= off && o < off + len) { if (a < 0) { a = i; start = row } b = i }
    row += Number(rg.num_rows)
  })
  let end = start
  for (let i = a; i <= b; i++) end += Number(md.row_groups[i].num_rows)
  return { groups: b - a + 1, rowStart: start, rowEnd: end }
}

async function one(r2: R2Bucket, file: string, r: Range, decode: boolean) {
  const m = await meta(r2, file)
  const t0 = Date.now()
  const buf = await (await r2.get(`proto/static/${file}`, { range: { offset: r.off, length: r.len } }))!.arrayBuffer()
  const t1 = Date.now()
  const out = { q: r.q, bytes: buf.byteLength, fetch_ms: t1 - t0 }
  if (!decode) return out
  const { groups, rowStart, rowEnd } = rowSpan(m.md, r.off, r.len)
  const file_ = {
    byteLength: m.size,
    slice(s: number, e?: number) {
      const end = e ?? m.size
      if (s >= r.off && end <= r.off + r.len) return buf.slice(s - r.off, end - r.off)
      if (s >= m.footOff) return m.foot.slice(s - m.footOff, end - m.footOff)
      throw new Error(`read outside spans ${s}-${end}`)
    },
  }
  const rows = await parquetReadObjects({ file: file_, metadata: m.md, rowStart, rowEnd, columns: ['s', 'path', 'vf', 'vt', 'size', 'n_files'], compressors }) as { s: string, path: string }[]
  const key = r.q.slice(0, 24)
  let hits = 0
  for (const x of rows) if (x.s.startsWith(key) && x.path.slice(x.path.lastIndexOf('/') + 1).toLowerCase().includes(r.q)) hits++
  const t2 = await tick()
  return { ...out, groups, rows: rows.length, hits, decode_ms: t2 - t1 }
}

export async function onRequestPost(ctx: Ctx & { env: Env }): Promise<Response> {
  const identity = await requireViewer({ request: ctx.request, env: { ...ctx.env, PUBLIC_READ: undefined } })
  if (identity instanceof Response) return identity
  const r2 = ctx.env.INDEX_R2
  if (ctx.env.STATIC_BENCH !== '1' || !r2) return json({ error: 'not enabled' }, 404)
  const { file, ranges, mode = 'seq', decode = false } = await ctx.request.json() as { file: string, ranges: Range[], mode?: string, decode?: boolean }
  const t0 = Date.now()
  const m = await meta(r2, file)
  const t1 = Date.now()
  const results = mode === 'par'
    ? await Promise.all(ranges.map(r => one(r2, file, r, false)))
    : await (async () => { const o = []; for (const r of ranges) o.push(await one(r2, file, r, decode)); return o })()
  const t2 = await tick()
  const cf = (ctx.request as { cf?: { colo?: string } }).cf
  return json({ colo: cf?.colo, meta: { cached: m.cached, fetch_ms: m.fetch_ms, parse_ms: m.parse_ms, footer_bytes: m.foot.byteLength, meta_ms: t1 - t0 }, total_ms: t2 - t1, results })
}

export async function onRequestGet(ctx: Ctx & { env: Env }): Promise<Response> {
  const identity = await requireViewer({ request: ctx.request, env: { ...ctx.env, PUBLIC_READ: undefined } })
  if (identity instanceof Response) return identity
  const r2 = ctx.env.INDEX_R2
  if (ctx.env.STATIC_BENCH !== '1' || !r2) return json({ error: 'not enabled' }, 404)
  const url = new URL(ctx.request.url), key = (url.searchParams.get('q') ?? '').toLowerCase(), dates = url.searchParams.getAll('d')
  if ([...key].length < 3 || !dates.length || dates.some(d => !isScanId(d))) return json({ error: 'q (≥ 3 characters) and d=YYYY-MM-DD[THHMM] required' }, 400)
  const t0 = Date.now()
  const { io, answer } = await names(r2, staticGen(ctx.env)).answer(key, dates)
  const total_ms = await tick() - t0
  const answers = Object.fromEntries(Object.entries(answer!.answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))
  const cf = (ctx.request as { cf?: { colo?: string } }).cf
  return json({ q: key, colo: cf?.colo, io, total_ms, rows_matching: answer!.rows_matching, answers })
}
