/** The digest's image (`_lib/og/digestCard.ts`), drawn by the site so its map
 * is the in-app diff's: the job posts the month's points and the headline
 * interval, and gets the PNG back to host beside the Slack thread.
 *
 *   POST /api/digest-image {title, note, month: 'YYYY-MM', points: [{scan, bytes}],
 *                           quota?: {bytes, name}, diff?: {from, to, path, title}}
 *     → image/png (1.5× the 900px-wide layout)
 *
 * Viewer-gated (the job's grant); nothing cached — one render a run. */
import { type Env, requireViewer } from '../_lib/auth.js'
import { ivRetry, storeReady, withPathStore } from '../_lib/index.js'
import { ATTEN_DEFAULT, buildDiff, MIN_AREA_DEFAULT } from '../_lib/view.js'
import { digestSvg, DIGEST_W, type DigestPoint } from '../_lib/og/digestCard.js'
import { ensureWasm, svgToPng } from '../_lib/og/render.js'
import { fonts, type OgEnv } from '../_lib/og/serve.js'
import RESVG from '../_lib/og/vendor/resvg.wasm'
import { diffRoot } from '../../src/diffStyle.js'
import { isScanId, scanTime } from '../../src/scanSlug.js'

export const ZOOM = 1.5
const MAP_W = DIGEST_W - 48
const MAP_H = 440

interface Body {
  title?: unknown
  note?: unknown
  month?: unknown
  points?: { scan?: unknown; bytes?: unknown }[]
  quota?: { bytes?: unknown; name?: unknown } | null
  diff?: { from?: unknown; to?: unknown; path?: unknown; title?: unknown } | null
}

const bad = (msg: string) => new Response(msg, { status: 400 })

export const onRequestPost = async (ctx0: { request: Request; env: OgEnv }): Promise<Response> => {
  const ctx = (await withPathStore(ctx0 as { request: Request; env: Env })) as { request: Request; env: OgEnv }
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const b = (await ctx.request.json().catch(() => null)) as Body | null
  if (!b || typeof b.title !== 'string' || typeof b.note !== 'string') return bad('title and note required')
  const m = typeof b.month === 'string' ? /^(\d{4})-(\d{2})$/.exec(b.month) : null
  if (!m) return bad('month: YYYY-MM')
  const points: DigestPoint[] = []
  for (const p of b.points ?? []) {
    if (typeof p.scan !== 'string' || !isScanId(p.scan) || typeof p.bytes !== 'number') return bad('points: [{scan, bytes}]')
    points.push({ t: scanTime(p.scan), bytes: p.bytes })
  }
  points.sort((x, y) => x.t - y.t)
  const [y, mo] = [Number(m[1]), Number(m[2]) - 1]
  // The month frame (scans fill it in left → right), widened to a lead-in scan before it.
  const from = Math.min(Date.UTC(y, mo, 1) - 16 * 3600_000, ...points.map(p => p.t))
  const to = Date.UTC(y, mo + 1, 1) - 8 * 3600_000
  const quota = b.quota && typeof b.quota.bytes === 'number' && typeof b.quota.name === 'string' ? { bytes: b.quota.bytes, name: b.quota.name } : null
  let diff = null
  if (b.diff) {
    const { from: f, to: t, path, title } = b.diff
    if (typeof f !== 'string' || typeof t !== 'string' || !isScanId(f) || !isScanId(t) || f >= t) return bad('diff: from < to, scan ids')
    if (typeof path !== 'string' || path.includes('..') || path.startsWith('/')) return bad('diff: bad path')
    if (typeof title !== 'string') return bad('diff: title required')
    if (!storeReady(ctx.env)) return new Response('diff not configured (missing index store creds)', { status: 503 })
    const data = await ivRetry(() => buildDiff(ctx.env, { from: f, to: t, path, w: MAP_W, h: MAP_H, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, top: 500 }))
    const built = diffRoot({ ...data, prev: f, curr: t }, 'delta', true, !path, path || 'all')
    diff = built ? { root: built.root, title } : null
  }
  const svg = digestSvg({ title: b.title, note: b.note, points, from, to, quota, diff })
  await ensureWasm(RESVG)
  const png = svgToPng(svg, await fonts(ctx.env, new URL(ctx.request.url).origin), ZOOM)
  return new Response(png, { headers: { 'content-type': 'image/png', 'cache-control': 'no-store' } })
}
