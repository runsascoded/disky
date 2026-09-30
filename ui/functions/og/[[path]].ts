/** `GET /og/<scheme>/<bucket>/<path…>` — the link-card treemap image for a scan
 *  uri (referenced by the og tags `_middleware.ts` injects). Resolution order:
 *
 *    1. R2 `og/<key>.jpg`   — refreshed daily from the live treemap by
 *                             `rescan-demo.yml` (freshest; tracks re-scans).
 *    2. static `/_og/<key>.jpg` — seed cards bundled with the deploy, so a
 *                             fresh project serves per-bucket cards on day one.
 *    3. edge render (tier B) — squarify + resvg-wasm over the subtree, for any
 *                             uri with a covering scan (drilled sub-paths).
 *    4. static `/og.jpg`    — the site-default card, when all else misses.
 *
 *  The render (3) is guarded: any failure falls through to (4), so a card is
 *  always returned. The wasm + Inter faces are served as static assets
 *  (`/_wasm`, `/_fonts`) and fetched once per isolate. */
import type { Env } from '../../cfn/env'
import { findCovering, getScans } from '../../cfn/manifests'
import { keyToUri } from '../../cfn/og'
import { ensureWasm, svgToPng } from '../../cfn/ogRender'
import { treemapCardSvg } from '../../cfn/ogSvg'
import { isSliceError, readScanSlice } from '../../cfn/scanRead'
// Imported as a build-time-compiled module (see `cfn/wasm.d.ts`): Workers can't
// compile wasm from fetched bytes at runtime.
import resvgWasm from '../../cfn/vendor/resvg.wasm'

const imgHeaders = (contentType: string) => ({ 'content-type': contentType, 'cache-control': 'public, max-age=3600' })

let fontsPromise: Promise<Uint8Array[]> | null = null
function loadFonts(base: string): Promise<Uint8Array[]> {
  if (!fontsPromise) {
    fontsPromise = Promise.all(
      ['/_fonts/Inter-400.ttf', '/_fonts/Inter-600.ttf'].map(async p =>
        new Uint8Array(await (await fetch(new URL(p, base))).arrayBuffer()),
      ),
    )
  }
  return fontsPromise
}

/** Render the treemap card for `uri` at the edge, or null if no scan covers it
 *  or it has no drawable children. */
async function renderCard(env: Env, base: string, uri: string): Promise<Uint8Array | null> {
  const scan = findCovering(await getScans(env), uri)
  if (!scan) return null
  const slice = await readScanSlice(env, scan, uri, 1)
  if (isSliceError(slice)) return null
  const children = slice.rows
    .filter(r => r.depth === 1 && (r.size ?? 0) > 0)
    .map(r => ({ name: r.path.split('/').filter(Boolean).pop() ?? r.path, size: r.size as number }))
  if (!children.length) return null

  const svg = treemapCardSvg({
    uri,
    children,
    total: slice.root.size ?? undefined,
    itemCount: slice.root.n_desc ?? undefined,
  })
  await ensureWasm(resvgWasm)
  return svgToPng(svg, await loadFonts(base))
}

export const onRequestGet: PagesFunction<Env> = async ({ params, request, env }) => {
  const segs = (Array.isArray(params.path) ? params.path : [params.path])
    .filter(Boolean)
    .map(s => decodeURIComponent(s as string))
  const key = segs.join('/')

  const obj = await env.SCANS.get(`og/${key}.jpg`)
  if (obj) return new Response(obj.body, { headers: imgHeaders('image/jpeg') })

  // A missing static asset resolves to the SPA shell (200 text/html), not a
  // 404 — so trust the seed only when it actually came back as an image.
  const seeded = await fetch(new URL(`/_og/${key}.jpg`, request.url))
  if (seeded.ok && (seeded.headers.get('content-type') ?? '').startsWith('image/')) {
    return new Response(seeded.body, { headers: imgHeaders('image/jpeg') })
  }

  try {
    const uri = keyToUri(segs)
    if (uri) {
      const png = await renderCard(env, request.url, uri)
      if (png) return new Response(png, { headers: imgHeaders('image/png') })
    }
  } catch (e) {
    console.error('og edge render failed', e)
  }
  return fetch(new URL('/og.jpg', request.url))
}
