/** Read a scan blob (`tree.parquet`) straight from an object store with the
 *  pushdown the Flask server uses: rows are sorted `(depth, path)` in bounded
 *  row groups, so a `depth ≤ d` + path-prefix request prunes to a handful of
 *  groups via the footer's min/max stats, each fetched with a range read. No
 *  server process, no whole-file download — a directory listing of a 7M-row
 *  scan touches a few 64K-row groups.
 *
 *  `uri` is not read: it's the longest column (absolute paths) and is derived
 *  as `<scan root>/<path>` by the caller, as the engines derive it. */
import { parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import type { AsyncBuffer, FileMetaData } from 'hyparquet'
import { compressors } from './zstd.js'

/** A scan row as stored (`storage/base.py` columns, minus `uri`). */
export interface TreeRow {
  path: string
  size: number | null
  mtime: number | null
  mtime_mean?: number | null
  kind: 'file' | 'dir'
  parent: string | null
  n_desc: number | null
  n_children: number | null
  depth: number
  /** Hybrid chunk pointer (`storage/hybrid.py`): a `<uuid>.parquet` basename
   *  whose blob holds this dir's subtree, when it was chunked out (≥100K desc).
   *  Present only on chunked blobs; `null`/absent otherwise. */
  child_scan_id?: string | null
}

const BASE_COLS = ['path', 'size', 'mtime', 'kind', 'parent', 'n_desc', 'n_children', 'depth']

export interface Query {
  /** Keep rows with `depth <= maxDepth`. */
  maxDepth: number
  /** Keep the row at `prefix` and its descendants (`null`: whole tree). */
  prefix?: string | null
}

export interface Run { rowStart: number; rowEnd: number }

/** `[lo, hi)` holding exactly the strings that start with `prefix + '/'` —
 *  `'0' === chr(ord('/') + 1)`, so nothing sorts between `pfx/…` and `pfx0`
 *  (`storage/base.py` `path_prefix_bounds`). */
export const prefixBounds = (prefix: string): [string, string] => [prefix + '/', prefix + '0']

/** An `AsyncBuffer` over an R2 object: every `slice` is one range GET. */
export function r2Buffer(bucket: R2Bucket, key: string, byteLength: number): AsyncBuffer {
  return {
    byteLength,
    async slice(start, end) {
      const length = (end ?? byteLength) - start
      const obj = await bucket.get(key, { range: { offset: start, length } })
      if (!obj) throw new Error(`missing object ${key}`)
      return obj.arrayBuffer()
    },
  }
}

const decoder = new TextDecoder()
const stat = (v: unknown): string | number | undefined =>
  v == null ? undefined
    : v instanceof Uint8Array ? decoder.decode(v)
    : typeof v === 'bigint' ? Number(v)
    : (v as string | number)

function columnStats(rg: FileMetaData['row_groups'][number], name: string): { min?: string | number; max?: string | number } {
  const col = rg.columns.find(c => c.meta_data?.path_in_schema?.[0] === name)
  const s = col?.meta_data?.statistics
  return { min: stat(s?.min_value ?? s?.min), max: stat(s?.max_value ?? s?.max) }
}

export const hasColumn = (meta: FileMetaData, name: string): boolean =>
  meta.schema.some(e => e.name === name)

/** Row-index runs of the groups that can hold matching rows, adjacent groups
 *  merged. A group is kept unless its stats prove it empty for the query:
 *  every row deeper than `maxDepth`, or every path outside `[prefix, hi)` —
 *  the prefix row itself sorts just before its descendants, so one range
 *  covers both (strays like `pfx-x` are dropped by the exact filter). */
export function selectRuns(meta: FileMetaData, q: Query): Run[] {
  const runs: Run[] = []
  const hi = q.prefix ? prefixBounds(q.prefix)[1] : null
  let offset = 0
  for (const rg of meta.row_groups) {
    const n = Number(rg.num_rows)
    const start = offset
    offset += n
    const d = columnStats(rg, 'depth')
    if (d.min !== undefined && Number(d.min) > q.maxDepth) continue
    if (q.prefix) {
      const p = columnStats(rg, 'path')
      if (p.max !== undefined && String(p.max) < q.prefix) continue
      if (p.min !== undefined && String(p.min) >= hi!) continue
    }
    const last = runs[runs.length - 1]
    if (last && last.rowEnd === start) last.rowEnd = start + n
    else runs.push({ rowStart: start, rowEnd: start + n })
  }
  return runs
}

const num = (v: unknown): number | null => (v == null ? null : Number(v))
const decoder2 = new TextDecoder()
/** A parquet string cell → JS string (hyparquet hands back `Uint8Array` for
 *  BYTE_ARRAY columns), or null. */
const str = (v: unknown): string | null =>
  v == null ? null : v instanceof Uint8Array ? decoder2.decode(v) : String(v)

/** The rows matching `q`, in file order (`(depth, path)`). */
export async function readRows(file: AsyncBuffer, q: Query, metadata?: FileMetaData): Promise<TreeRow[]> {
  const meta = metadata ?? await parquetMetadataAsync(file)
  const meanMtime = hasColumn(meta, 'mtime_mean')
  const chunked = hasColumn(meta, 'child_scan_id')
  const columns = [...BASE_COLS, ...(meanMtime ? ['mtime_mean'] : []), ...(chunked ? ['child_scan_id'] : [])]
  const [lo, hi] = q.prefix ? prefixBounds(q.prefix) : [null, null]
  const out: TreeRow[] = []
  for (const { rowStart, rowEnd } of selectRuns(meta, q)) {
    const rows = await parquetReadObjects({ file, metadata: meta, columns, rowStart, rowEnd, compressors })
    for (const r of rows) {
      const depth = Number(r.depth)
      if (depth > q.maxDepth) continue
      const path = String(r.path)
      if (q.prefix && !(path === q.prefix || (path >= lo! && path < hi!))) continue
      const row: TreeRow = {
        path,
        size: num(r.size),
        mtime: num(r.mtime),
        kind: r.kind as 'file' | 'dir',
        parent: r.parent == null ? null : String(r.parent),
        n_desc: num(r.n_desc),
        n_children: num(r.n_children),
        depth,
      }
      if (meanMtime) row.mtime_mean = num(r.mtime_mean)
      if (chunked) row.child_scan_id = str(r.child_scan_id) || null
      out.push(row)
    }
  }
  return out
}

/**
 * `path → child_scan_id` for a hybrid blob's chunk-pointer rows (empty when the
 * blob has no `child_scan_id` column — flat `reduce` output, or non-chunked).
 * A chunk is a depth-1 dir of its blob (`storage/hybrid.py` splits depth-1 dirs
 * with ≥100K descendants), so only depth-1 rows are scanned — cheap even on a
 * multi-million-row blob (`diff._chunk_map`, read-side). */
export async function readChunkPointers(file: AsyncBuffer, metadata?: FileMetaData): Promise<Map<string, string>> {
  const meta = metadata ?? await parquetMetadataAsync(file)
  const out = new Map<string, string>()
  if (!hasColumn(meta, 'child_scan_id')) return out
  for (const { rowStart, rowEnd } of selectRuns(meta, { maxDepth: 1 })) {
    const rows = await parquetReadObjects({ file, metadata: meta, columns: ['path', 'child_scan_id', 'depth'], rowStart, rowEnd, compressors })
    for (const r of rows) {
      if (Number(r.depth) !== 1) continue
      const ref = str(r.child_scan_id)
      if (ref) out.set(String(r.path), ref)
    }
  }
  return out
}
