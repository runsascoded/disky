/** `GET /og/deploy.png?[store=…&][theme=light&][c=rrggbb&]v=…&sig=…`: the deployment's status card (`deployData.ts`),
 * drawn from the store's runs and series when fetched. Only a valid `sig` (`deployCardUrl`, minted for the job's grant
 * by `GET /api/scan-runs/card-url`) draws it; without one, or with a bad or expired one, the scan runs page's generic
 * card, never an error. A colo cache holds each render for a day per signed view and build, so the burst of fetches a
 * post makes is one render. */
import { DEPLOY_ZOOM, deployCardSvg } from '@disk-tree/pages-kit/og/deployCard'
import RESVG from './vendor/resvg.wasm'
import { BUILD } from '../build.js'
import { withPathStore } from '../index.js'
import { withStore } from '../stores.js'
import { DEFAULT_COLOR, DEPLOY_KIND, deployData, type DeployEnv } from './deployData.js'
import { genericImageOf } from './policy.js'
import { ensureWasm, svgToPng } from './render.js'
import { deploymentKey, fonts, type OgEnv, serveCard } from './serve.js'
import { canonical, resolveImage } from './sign.js'

type Ctx = { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }

export async function serveDeployCard(ctx: Ctx): Promise<Response> {
  const url = new URL(ctx.request.url)
  const now = Math.floor(Date.now() / 1000)
  const keyP = deploymentKey(ctx.env)
  const r = await resolveImage(keyP ? await keyP : null, url, now)
  if (!r || r.kind !== DEPLOY_KIND) return new Response('not a card', { status: 404 })
  if (r.tier !== 'full') {
    console.log(`og/deploy.png: anonymous (${r.why ?? 'no sig'})`)
    return serveCard({ ...ctx, request: new Request(url.origin + genericImageOf('scans'), { signal: ctx.request.signal }) })
  }
  const cacheKey = new Request(`${url.origin}/og/${DEPLOY_KIND}.png?${canonical(r.params)}&bld=${BUILD.sha}`)
  const hit = await caches.default.match(cacheKey)
  if (hit) return withTier(hit)
  const scoped = withStore(ctx)
  if (scoped instanceof Response) return scoped
  const { env } = await withPathStore(scoped)
  const data = await deployData(env as DeployEnv, url.origin, now)
  const c = /^[0-9a-f]{6}$/.test(r.params.c ?? '') ? `#${r.params.c}` : DEFAULT_COLOR
  const svg = deployCardSvg({ ...data, color: c, theme: r.params.theme === 'light' ? 'light' : 'dark', now })
  await ensureWasm(RESVG)
  const png = svgToPng(svg, await fonts(ctx.env, url.origin), DEPLOY_ZOOM)
  const res = new Response(png, { headers: { 'content-type': 'image/png', 'cache-control': 'public, max-age=86400' } })
  const put = caches.default.put(cacheKey, res.clone())
  if (ctx.waitUntil) ctx.waitUntil(put)
  else await put
  return withTier(res)
}

/** A full card as a fetcher gets it: never stored by a shared cache (its URL is the credential). */
function withTier(res: Response): Response {
  const h = new Headers(res.headers)
  h.set('cache-control', 'private, no-store')
  h.set('x-og-tier', 'full')
  return new Response(res.body, { status: res.status, headers: h })
}
