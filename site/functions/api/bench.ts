/** Decode benchmarks (any `gcs` reader: counts, timings, one sample row), run in the real Pages Function environment
 * against real objects in GCS — the laptop can't stand in for the edge (its
 * parquet decode is ~14× faster, and its fetches are bandwidth-bound where
 * the edge's are latency- and CPU-bound). CPU per request comes from the
 * deployment log tail (`wrangler pages deployment tail <id> --format json`,
 * `cpuTime`); this endpoint reports counts, bytes and wall phases.
 *
 *   GET /api/bench?mode=groups&date=<scan>&variant=coarse20   today's reader: every group of a tier
 *   GET /api/bench?mode=json|rows|bin&key=bench/<scan>/<blob>   a gzip tier blob: inflate + parse + shape
 *   GET /api/bench?mode=lookup&date=<scan>&paths=a,b,…           point lookups via today's `readAsks`
 *   GET /api/bench?mode=pagelookup&key=bench/<scan>/<pq>&paths=… point lookups via page index + filter
 */
import { parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import { withStore } from '../_lib/stores.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { makeStore, openIndex, readAsks, readRects, type Row } from '../_lib/index.js'
import { compressors } from '../_lib/zstd.js'

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } })

async function inflate(gz: Uint8Array): Promise<ArrayBuffer> {
  const ds = new DecompressionStream('gzip')
  const stream = new Response(gz.buffer.slice(gz.byteOffset, gz.byteOffset + gz.byteLength) as ArrayBuffer).body!.pipeThrough(ds)
  return await new Response(stream).arrayBuffer()
}

export const onRequestGet = async (ctx0: { request: Request; env: Env }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx as never)
  if (gated instanceof Response) return gated
  const url = new URL(ctx.request.url)
  const mode = url.searchParams.get('mode') ?? ''
  const date = url.searchParams.get('date') ?? ''
  const key = url.searchParams.get('key') ?? ''
  const variant = url.searchParams.get('variant') ?? 'coarse20'
  const paths = (url.searchParams.get('paths') ?? '').split(',').filter(Boolean)
  const store = makeStore(ctx.env)
  const t: Record<string, number> = {}
  const t0 = Date.now()
  const lap = (k: string, s: number) => { t[k] = Date.now() - s }
  try {
    if (mode === 'groups') {
      const h = await openIndex(ctx.env, date, variant)
      const s = Date.now()
      const rows = await readRects(h, [{ dLo: 1, dHi: 1e9, pLo: '', pHi: '￿' }])
      lap('read', s)
      return json({ mode, variant, rows: rows.length, t, total: Date.now() - t0 })
    }
    if (mode === 'json' || mode === 'rows' || mode === 'bin') {
      let s = Date.now()
      const got = await store.get(key)
      lap('fetch', s)
      s = Date.now()
      const buf = await inflate(got.bytes)
      lap('inflate', s)
      s = Date.now()
      let rows: Row[]
      if (mode === 'bin') {
        const dv = new DataView(buf)
        const hl = dv.getUint32(0, true)
        const hdr = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hl))) as { n: number; sections: { name: string; dtype: string; bytes: number }[]; paths: number; usrs: number }
        let off = 4 + hl
        const cols: Record<string, Float64Array | Uint8Array | Int32Array> = {}
        for (const sec of hdr.sections) {
          // copy into an aligned buffer (sections aren't 8-byte aligned in the blob)
          const bytes = buf.slice(off, off + sec.bytes)
          cols[sec.name] = sec.dtype === 'float64' ? new Float64Array(bytes) : sec.dtype === 'int32' ? new Int32Array(bytes) : new Uint8Array(bytes)
          off += sec.bytes
        }
        const dec = new TextDecoder()
        const ps = dec.decode(new Uint8Array(buf, off, hdr.paths)).split('\n')
        const us = dec.decode(new Uint8Array(buf, off + hdr.paths, hdr.usrs)).split('\n')
        lap('parse', s)
        s = Date.now()
        const b = cols.b as Float64Array, o = cols.o as Float64Array, wts = cols.wts as Float64Array, wb = cols.wb as Float64Array
        const c2 = cols.c2 as Float64Array, c3 = cols.c3 as Float64Array, c4 = cols.c4 as Float64Array, a = cols.a as Int32Array, depth = cols.depth as Uint8Array
        rows = new Array(hdr.n)
        for (let i = 0; i < hdr.n; i++) rows[i] = { path: ps[i], depth: depth[i], usr: us[i] || null, kind: 'dir', size: b[i], n_files: o[i], n_children: null, n_desc: null, mtime: null, mtime_mean: wb[i] > 0 ? wts[i] / wb[i] : null, mtime_w: wb[i], cls2: c2[i], cls3: c3[i], cls4: c4[i], last_read: a[i] < 0 ? null : a[i] }
        lap('shape', s)
      } else {
        const text = new TextDecoder().decode(buf)
        const parsed = JSON.parse(text)
        lap('parse', s)
        s = Date.now()
        if (mode === 'json') {
          const c = parsed as Record<string, unknown[]>
          const n = c.path.length
          rows = new Array(n)
          for (let i = 0; i < n; i++) rows[i] = { path: c.path[i] as string, depth: c.depth[i] as number, usr: (c.usr[i] as string | null) ?? null, kind: 'dir', size: c.b[i] as number, n_files: c.o[i] as number, n_children: null, n_desc: null, mtime: null, mtime_mean: (c.wb[i] as number) > 0 ? (c.wts[i] as number) / (c.wb[i] as number) : null, mtime_w: c.wb[i] as number, cls2: c.c2[i] as number, cls3: c.c3[i] as number, cls4: c.c4[i] as number, last_read: (c.a[i] as number | null) ?? null }
        } else {
          const r = (parsed as { rows: unknown[][] }).rows
          rows = r.map(v => ({ path: v[0] as string, depth: v[1] as number, usr: (v[2] as string | null) ?? null, kind: 'dir' as const, size: v[3] as number, n_files: v[4] as number, n_children: null, n_desc: null, mtime: null, mtime_mean: (v[6] as number) > 0 ? (v[5] as number) / (v[6] as number) : null, mtime_w: v[6] as number, cls2: v[7] as number, cls3: v[8] as number, cls4: v[9] as number, last_read: (v[10] as number | null) ?? null }))
        }
        lap('shape', s)
      }
      return json({ mode, key, gzBytes: got.bytes.byteLength, rawBytes: buf.byteLength, rows: rows.length, sample: rows[rows.length >> 1], t, total: Date.now() - t0 })
    }
    if (mode === 'lookup') {
      const h = await openIndex(ctx.env, date, variant)
      const s = Date.now()
      const want = new Set(paths)
      const { rows, groups } = await readAsks(h, paths.map(p => ({ depth: p.split('/').length, path: p })), r => want.has(r.path), { maxGroups: 250 })
      lap('read', s)
      return json({ mode, variant, asks: paths.length, found: rows.length, groups, t, total: Date.now() - t0 })
    }
    if (mode === 'pagelookup') {
      // the file, with a real byteLength (the footer is parsed from the tail)
      let fetched = 0, fetches = 0
      const size = (await store.get(key, { offset: 0, length: 1 })).totalSize!
      const file = {
        byteLength: size,
        slice: async (a: number, b?: number) => {
          const end = b ?? size
          const r = await store.get(key, { offset: a, length: end - a })
          fetched += r.bytes.byteLength; fetches++
          return r.bytes.buffer.slice(r.bytes.byteOffset, r.bytes.byteOffset + r.bytes.byteLength) as ArrayBuffer
        },
      }
      let s = Date.now()
      const metadata = await parquetMetadataAsync(file)
      lap('footer', s)
      const footerBytes = fetched; fetched = 0; fetches = 0
      // the candidate row group per ask, by the footer's path min/max (what D1 spans give today)
      const pathCol = metadata.schema.findIndex(e => e.name === 'path') - 1
      const groupsFor = (p: string) => metadata.row_groups.map((rg, i) => ({ rg, i })).filter(({ rg }) => {
        const st = rg.columns[pathCol].meta_data?.statistics
        const lo = st?.min_value as string | undefined, hi = st?.max_value as string | undefined
        return lo != null && hi != null && lo <= p && p <= hi
      })
      s = Date.now()
      let found = 0, groupsRead = 0
      const usePageIndex = url.searchParams.get('pageindex') !== '0'
      for (const p of paths) {
        for (const { rg } of groupsFor(p)) {
          groupsRead++
          const md = { ...metadata, row_groups: [rg], num_rows: rg.num_rows }
          const rows = await parquetReadObjects({ file, metadata: md, filter: { path: { $eq: p } }, usePageIndex, compressors })
          found += rows.length
        }
      }
      lap('lookups', s)
      return json({ mode, key, asks: paths.length, found, groupsRead, usePageIndex, footerBytes, fetches, fetchedBytes: fetched, t, total: Date.now() - t0 })
    }
    return json({ error: 'mode=groups|json|rows|bin|lookup|pagelookup' }, 400)
  } catch (e) {
    return json({ error: String((e as Error).stack ?? e) }, 500)
  }
}
