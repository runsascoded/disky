/** Read one scan's slice at a uri — the shared core of `/api/scan` and
 *  `/api/compare`: resolve hybrid chunks, read the depth- and prefix-pruned
 *  rows of the blob that actually holds the path, and rebase them to the viewed
 *  uri (its row becomes `.`, descendants lose the prefix). Chunk-stub children
 *  get their top-level breakdown spliced in (`server.py` child-scan loading). */
import type { Env } from './env'
import { scansPrefix } from './env'
import type { Scan } from './manifests'
import { r2Buffer, readChunkPointers, readRows } from './parquet'
import type { TreeRow } from './parquet'
import { metaFor } from './groups'

/** A scan row rebased to the viewed uri: `path`/`parent` relative (`.` = uri),
 *  `depth` relative, `uri` absolute. */
export type ApiRow = Omit<TreeRow, 'parent'> & { parent: string | null; uri: string; scanned?: boolean; scan_time?: string }

const rowUri = (scan: Scan, path: string): string =>
  path === '.' ? scan.path : scan.path === '/' ? `/${path}` : `${scan.path}/${path}`

export const depthOf = (rel: string): number => (rel === '.' || rel === '' ? 0 : rel.split('/').length)

/** Scan-relative path of the viewed `uri` within `scan` (`.` when they match). */
export const relOf = (scan: Scan, uri: string): string =>
  scan.path === uri ? '.' : uri.slice(scan.path.replace(/\/$/, '').length + 1)

interface Resolved { blob: string; rebased: string; chunkRootRel: string }

/** Walk each blob's chunk-pointer map along `rel` to the blob holding it,
 *  recursing through nested chunks (`diff.resolve_chunk_for_path`). A missing
 *  chunk blob is treated as unchunked (best-effort). */
async function resolveChunk(env: Env, blob: string, rel: string, chunkRootRel = ''): Promise<Resolved> {
  if (!rel || rel === '.') return { blob, rebased: '.', chunkRootRel }
  const key = scansPrefix(env) + blob
  const head = await env.SCANS.head(key)
  if (!head) return { blob, rebased: rel, chunkRootRel }
  const pointers = await readChunkPointers(r2Buffer(env.SCANS, key, head.size), await metaFor(env.SCANS, key))
  if (pointers.size === 0) return { blob, rebased: rel, chunkRootRel }
  const parts = rel.split('/')
  for (let i = 0; i < parts.length; i++) {
    const anc = parts.slice(0, i + 1).join('/')
    const ref = pointers.get(anc)
    if (ref && (await env.SCANS.head(scansPrefix(env) + ref))) {
      const remaining = i + 1 < parts.length ? parts.slice(i + 1).join('/') : '.'
      return resolveChunk(env, ref, remaining, chunkRootRel ? `${chunkRootRel}/${anc}` : anc)
    }
  }
  return { blob, rebased: rel, chunkRootRel }
}

/** Re-prefix a chunk-relative row into scan coordinates (chunk root at
 *  scan-relative `chunkRootRel`, depth `rootDepth`); identity when unchunked. */
function toScanAbs(r: TreeRow, chunkRootRel: string, rootDepth: number): TreeRow {
  if (!chunkRootRel) return r
  const path = r.path === '.' ? chunkRootRel : `${chunkRootRel}/${r.path}`
  const parent =
    r.parent == null || r.parent === '' ? null
    : r.parent === '.' ? chunkRootRel
    : `${chunkRootRel}/${r.parent}`
  return { ...r, path, parent, depth: r.depth + rootDepth }
}

export interface SliceError { error: string; status: number; extra?: Record<string, unknown> }
export interface Slice { root: ApiRow; rows: ApiRow[] }
export const isSliceError = (s: Slice | SliceError): s is SliceError => 'error' in s

/**
 * The rows of `scan` under `uri` to `depth` levels, rebased to `uri`. `rows`
 * are the descendants (relative depth 1..depth), `root` the `.` row. Follows
 * hybrid chunks (into a chunk when `uri` is inside one, and splicing a chunk-
 * stub child's depth-1 breakdown). Returns a `SliceError` on a missing blob or
 * a uri absent from the scan.
 */
export async function readScanSlice(env: Env, scan: Scan, uri: string, depth: number): Promise<Slice | SliceError> {
  const rel = relOf(scan, uri)
  const viewedDepth = depthOf(rel)

  const { blob, rebased, chunkRootRel } = await resolveChunk(env, scan.blob, rel)
  const rootDepth = depthOf(chunkRootRel)
  const key = scansPrefix(env) + blob
  const head = await env.SCANS.head(key)
  if (!head) return { error: `blob missing: ${blob}`, status: 500 }
  const rawRows = await readRows(r2Buffer(env.SCANS, key, head.size), {
    maxDepth: depthOf(rebased) + depth,
    prefix: rebased === '.' ? null : rebased,
  }, await metaFor(env.SCANS, key))
  const stored = rawRows.map(r => toScanAbs(r, chunkRootRel, rootDepth))

  const rootRow = stored.find(r => r.path === rel)
  if (!rootRow) return { error: 'URI not found in scan', status: 404, extra: { uri, scan_path: scan.path } }

  const cut = rel === '.' ? 0 : rel.length + 1
  const toApi = (r: TreeRow): ApiRow => {
    const { parent, ...rest } = r
    const path = r.path === rel ? '.' : cut ? r.path.slice(cut) : r.path
    const relParent = path === '.' ? null : parent == null ? null : parent === rel ? '.' : cut && parent.startsWith(rel + '/') ? parent.slice(cut) : parent
    return { ...rest, path, parent: relParent, uri: rowUri(scan, r.path), depth: r.depth - viewedDepth }
  }
  const root = toApi(rootRow)
  const rows = stored.filter(r => r !== rootRow).map(toApi).filter(r => r.depth >= 1 && r.depth <= depth)

  // Chunk completeness: splice a chunk-stub child's depth-1 breakdown.
  const children = rows.filter(r => r.depth === 1)
  for (const c of children) {
    if (!c.child_scan_id || c.depth + 1 > depth) continue
    const cKey = scansPrefix(env) + c.child_scan_id
    const cHead = await env.SCANS.head(cKey)
    if (!cHead) continue
    const cRows = await readRows(r2Buffer(env.SCANS, cKey, cHead.size), { maxDepth: 1 }, await metaFor(env.SCANS, cKey))
    for (const cr of cRows) {
      if (cr.depth !== 1) continue
      rows.push({ ...cr, path: `${c.path}/${cr.path}`, parent: c.path, depth: c.depth + 1, uri: `${c.uri}/${cr.path}` })
    }
  }
  return { root, rows }
}
