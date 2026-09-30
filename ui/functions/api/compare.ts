import type { Env } from '../../cfn/env'
import { error, json, normUri } from '../../cfn/http'
import { getScans } from '../../cfn/manifests'
import type { Scan } from '../../cfn/manifests'
import { isSliceError, readScanSlice } from '../../cfn/scanRead'
import type { ApiRow } from '../../cfn/scanRead'
import { flatDiff, recursiveDiff } from '../../cfn/diff'
import type { RebasedRow } from '../../cfn/diff'

const DEFAULT_MAX_ROWS = 2000

const meta = (s: Scan, root: ApiRow | undefined) => ({
  id: s.id,
  time: s.time,
  // Compared-uri stats (the slice root), not the whole-scan root.
  size: root ? root.size : null,
  n_desc: root ? root.n_desc : null,
  scan_path: s.path,
})

/** `GET /api/compare?uri=&scan1=&scan2=[&recursive=1&min_frac=&budget=]` — the
 *  serverless analog of Flask `/api/compare`: read the depth-≤2 slice of both
 *  scans at `uri` (hybrid chunks followed) and diff them on the fly. No
 *  persisted index — the response carries no `index` field, so the client
 *  treats it as final (no poll). Recursive → the treemap frontier; otherwise →
 *  the flat one-level child diff (the table). */
export const onRequestGet: PagesFunction<Env> = async ({ request, env }) => {
  const params = new URL(request.url).searchParams
  const uri = normUri(params.get('uri'))
  const id1 = Number(params.get('scan1'))
  const id2 = Number(params.get('scan2'))
  const recursive = params.get('recursive') === '1'
  const minFrac = params.has('min_frac') ? Number(params.get('min_frac')) : undefined
  const maxRows = Number(params.get('max_rows') ?? DEFAULT_MAX_ROWS)

  if (!id1 || !id2) return error('scan1 and scan2 are required', 400)
  const scans = await getScans(env)
  const s1 = scans.find(s => s.id === id1)
  const s2 = scans.find(s => s.id === id2)
  if (!s1 || !s2) return error('scan not found', 404, { scan1: id1, scan2: id2 })

  // Depth-2 for the treemap frontier, depth-1 for the flat table.
  const depth = recursive ? 2 : 1
  const [a, b] = await Promise.all([readScanSlice(env, s1, uri, depth), readScanSlice(env, s2, uri, depth)])

  // A uri absent from a scan (a dir added/removed between the two) is an empty
  // side — its whole subtree is the change; only a *blob* error is fatal.
  for (const s of [a, b]) if (isSliceError(s) && s.status !== 404) return error(s.error, s.status, s.extra)
  if (isSliceError(a) && isSliceError(b)) return error('URI not found in either scan', 404, { uri })

  const rootA = isSliceError(a) ? undefined : a.root
  const rootB = isSliceError(b) ? undefined : b.root
  const aRows: RebasedRow[] = isSliceError(a) ? [] : [a.root, ...a.rows]
  const bRows: RebasedRow[] = isSliceError(b) ? [] : [b.root, ...b.rows]

  const scan1 = meta(s1, rootA), scan2 = meta(s2, rootB)

  if (recursive) {
    const { rows, unchanged, summary } = recursiveDiff(aRows, bRows, { uri, minFrac, maxRows })
    return json({ uri, recursive: true, scan1, scan2, rows, unchanged, summary })
  }
  const { rows, summary } = flatDiff(aRows, bRows, { uri, minFrac })
  return json({ uri, scan1, scan2, rows, summary })
}
