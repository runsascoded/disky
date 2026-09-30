import type { Env } from '../../cfn/env'
import { error, json, normUri } from '../../cfn/http'
import { findCovering, getScans } from '../../cfn/manifests'
import type { Scan } from '../../cfn/manifests'
import { isSliceError, readScanSlice } from '../../cfn/scanRead'
import type { ApiRow } from '../../cfn/scanRead'

const DEFAULT_DEPTH = 2
const DEFAULT_MAX_ROWS = 2000  // keep in sync with `server.DEFAULT_MAX_ROWS` / `ui/src/api.ts`

/** `GET /api/scan?uri=&depth=&max_rows=[&scan_id=]` — the Flask handler's
 *  scan-backed branch: the newest scan of `uri` or an ancestor, a depth- and
 *  prefix-pruned read of its blob (hybrid chunks followed, see `scanRead.ts`),
 *  paths rebased to `uri`. No filesystem fallback, no single-child auto-expand. */
export const onRequestGet: PagesFunction<Env> = async ({ request, env }) => {
  const params = new URL(request.url).searchParams
  const uri = normUri(params.get('uri'))
  const depth = Number(params.get('depth') ?? DEFAULT_DEPTH)
  const maxRows = Number(params.get('max_rows') ?? DEFAULT_MAX_ROWS)
  const scanId = params.get('scan_id')

  const scans = await getScans(env)
  let scan: Scan | null
  if (scanId) {
    scan = scans.find(s => s.id === Number(scanId)) ?? null
    if (scan && !(uri === scan.path || uri.startsWith(scan.path.replace(/\/$/, '') + '/'))) {
      return error(`Scan ${scanId} does not cover path ${uri}`, 400)
    }
  } else {
    scan = findCovering(scans, uri)
  }
  if (!scan) return error('No scan found for path', 404, { uri })

  const slice = await readScanSlice(env, scan, uri, depth)
  if (isSliceError(slice)) return error(slice.error, slice.status, slice.extra)
  const { root, rows: all } = slice
  const children = all.filter(r => r.depth === 1).map(r => ({ ...r, scanned: true, scan_time: scan.time }))

  let rows: ApiRow[] = all
  if (maxRows > 0 && all.length > maxRows) {
    // Top N by size, plus each kept row's ancestors so the treemap stays a tree.
    const byPath = new Map(all.map(r => [r.path, r]))
    const kept = [...all].sort((a, b) => (b.size ?? 0) - (a.size ?? 0)).slice(0, maxRows)
    const included = new Set(kept.map(r => r.path))
    for (const r of [...kept]) {
      for (let p = r.parent; p && p !== '.' && !included.has(p); ) {
        const pr = byPath.get(p)
        if (!pr) break
        kept.push(pr)
        included.add(p)
        p = pr.parent
      }
    }
    rows = kept
  }

  return json({
    root,
    children,
    rows,
    time: scan.time,
    scan_path: scan.path,
    scan_status: 'full',
    error_count: scan.error_count,
    error_paths: scan.error_paths,
    collapsed_rows: [],
  })
}
