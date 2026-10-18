/** The digest's image, drawn by the site: the job posts the month's points (and the headline interval, whose map is
 * the in-app diff's) and gets the PNG back to host beside the Slack thread. The request's two
 * kinds (`usage`: cw's line + quota + diff map; `tiers`: gcs's total line over stacked storage classes + diff map) are
 * `_lib/og/digestRequest.ts`'s; the cards are `_lib/og/digestCard.ts`'s.
 *
 *   POST /api/digest-image {kind?: 'usage' | 'tiers', title, note, month: 'YYYY-MM', points, …}
 *     → image/png (1.5× the 900px-wide layout)
 *
 * Viewer-gated (the job's grant); nothing cached — one render a run. */
import { type Env, requireViewer } from '../_lib/auth.js'
import { ivRefusal, ivRetry, storeReady, withPathStore } from '../_lib/index.js'
import { ATTEN_DEFAULT, buildDiff, MIN_AREA_DEFAULT } from '../_lib/view.js'
import { digestSvg, DIGEST_W, tiersSvg } from '../_lib/og/digestCard.js'
import { parseDigestRequest } from '../_lib/og/digestRequest.js'
import { ensureWasm, svgToPng } from '../_lib/og/render.js'
import { fonts, type OgEnv } from '../_lib/og/serve.js'
import RESVG from '../_lib/og/vendor/resvg.wasm'
import { diffRoot } from '../../src/diffStyle.js'

export const ZOOM = 1.5
const MAP_W = DIGEST_W - 48
const MAP_H = 440

export const onRequestPost = async (ctx0: { request: Request; env: OgEnv }): Promise<Response> => {
  const ctx = (await withPathStore(ctx0 as { request: Request; env: Env })) as { request: Request; env: OgEnv }
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const req = parseDigestRequest(await ctx.request.json().catch(() => null))
  if (typeof req === 'string') return new Response(req, { status: 400 })
  let diff = null
  if (req.diff) {
    const { from: f, to: t, path, title } = req.diff
    if (!storeReady(ctx.env)) return new Response('diff not configured (missing index store creds)', { status: 503 })
    let data
    try {
      data = await ivRetry(() => buildDiff(ctx.env, { from: f, to: t, path, w: MAP_W, h: MAP_H, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, top: 500 }))
    } catch (e) {
      // A scan a broken interval-store run cut out: 503 + `Retry-After`, the job retries.
      const r = ivRefusal(e)
      if (r) return r
      throw e
    }
    const built = diffRoot({ ...data, prev: f, curr: t }, 'delta', true, !path, path || 'all')
    diff = built ? { root: built.root, title } : null
  }
  const svg = req.kind === 'tiers' ? tiersSvg({ ...req, diff }) : digestSvg({ ...req, diff })
  await ensureWasm(RESVG)
  const png = svgToPng(svg, await fonts(ctx.env, new URL(ctx.request.url).origin), ZOOM)
  return new Response(png, { headers: { 'content-type': 'image/png', 'cache-control': 'no-store' } })
}
