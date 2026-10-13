/** The edge half of the cards (specs/done/dogi.md): the deployment's OG config,
 * page meta stamping, and `/og/<kind>.png` rendering with the colo cache. */
import RESVG from './vendor/resvg.wasm'
import { type Env, json } from '../auth.js'
import { ledgerHead } from '../ledger.js'
import { stampMeta } from '../unfurl.js'
import { cardSvg, type CardData } from './card.js'
import { mapCard, type Site } from './data.js'
import { stagedImageVersion } from '../stagedSlack.js'
import { assignmentsCard, stagedCard, userCard, usersCard } from './pages.js'
import { ensureWasm, FONT_FILES, svgToPng } from './render.js'
import { pageView, type OgKind } from './routes.js'
import { imagePath, ogKey, resolveImage, type OgTier } from './sign.js'
import { credLive, pageTier } from './tier.js'
import { imageParams, splitImageParams } from './cred.js'
import { warmUrls } from './warm.js'
import { anonymousPolicy, genericCard, genericImagePath, genericMeta, RENDER_VERSION, type PolicyEnv } from './policy.js'

import { canonId, loadRegistry } from '../identity.js'
import { openPlanId, planDigest, planItemLines } from '../plans.js'
import { NoScanMatch, resolveScan } from './data.js'
import { warmSubtree } from '../../api/subtree.js'
import { colorModeCodec } from '../../../src/colorMode.js'
import type { ColorMode } from '../../../src/types.js'

export type OgEnv = Env & PolicyEnv & {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  /** Stamp detailed/shape HTML metadata. Generic private metadata remains safe
   * even when unset; this does not disable direct image routes. */
  OG_CARDS?: string
  /** Match the frontend Store.defaultColor when configured; otherwise infer
   * owner vs tree from the scan's attribution, as the dashboard does. */
  OG_DEFAULT_COLOR?: string
}

const uncachedHtml = (response: Response): Response => {
  const headers = new Headers(response.headers)
  headers.set('cache-control', 'private, no-store')
  return new Response(response.body, { status: response.status, headers })
}

/** Called before route Functions: private anonymous unfurls must not read
 * staging/owner/scan data even to build their HTML descriptions. Only explicit
 * preview/share credentials are checked; browser sessions do not grant previews. */
export async function anonymousPage(env: OgEnv, url: URL): Promise<Response | null> {
  if (anonymousPolicy(env) !== 'generic') return null
  const pv = pageView(url, siteOf(env).name)
  if (!pv || !DRAWN.has(pv.kind)) return null
  if ((await pageTier(env, pv.kind, pv.params, url, now())).tier === 'full') return null
  const shell = await env.ASSETS.fetch(new Request(new URL('/', url)))
  return uncachedHtml(stampMeta(shell, genericMeta(env, url)))
}

/** The kinds this build draws; the rest keep their static card. */
export const DRAWN: ReadonlySet<OgKind> = new Set(['map', 'staged', 'users', 'user', 'assignments'])

/** Kinds whose page Function already stamps its own title and description
 * (`functions/staged.ts`, `users.ts`, `user/[id].ts`, `assignments.ts`): the
 * middleware only swaps their image. */
const OWN_TITLES: ReadonlySet<OgKind> = new Set(['staged', 'users', 'user', 'assignments'])

export const siteOf = (env: OgEnv): Site => {
  const c = colorModeCodec.decode(env.OG_DEFAULT_COLOR)
  return {
    name: env.ROOT_LABEL ?? 'storage', scheme: env.STORE_SCHEME ?? 'gs://',
    ...(c && ['tree', 'date', 'read', 'user'].includes(c) ? { defaultColor: c as ColorMode } : {}),
  }
}

let keyMemo: { secret: string; key: Promise<CryptoKey> } | null = null
/** The OG key for this deployment, or null when it has no `SESSION_SECRET`. */
export function deploymentKey(env: OgEnv): Promise<CryptoKey> | null {
  const s = env.SESSION_SECRET
  if (!s) return null
  if (keyMemo?.secret !== s) keyMemo = { secret: s, key: ogKey(s) }
  return keyMemo.key
}

const now = () => Math.floor(Date.now() / 1000)

/** The open plan's card version (`stagedImageVersion`: its digest and the
 * latest scan), so `/staged`'s image URL changes with every staging gesture
 * and every scan. Null when there's no plan. */
async function stagedVersion(env: OgEnv): Promise<string | null> {
  if (!env.DB) return null
  const plan = await openPlanId(env.DB).catch(() => null)
  if (plan == null) return null
  return stagedImageVersion(env, await planDigest(await planItemLines(env.DB, plan)))
}

/** Stamp a page's HTML with its card when it has one. */
export async function stampPage(env: OgEnv, url: URL, html: Response): Promise<Response> {
  const pv = pageView(url, siteOf(env).name)
  if (!pv || !DRAWN.has(pv.kind)) return html
  const { tier, day, cred } = await pageTier(env, pv.kind, pv.params, url, now())
  if (tier === 'anon' && anonymousPolicy(env) === 'generic') return uncachedHtml(stampMeta(html, genericMeta(env, url)))
  if (!env.OG_CARDS) return html
  const keyP = deploymentKey(env)
  if (tier === 'full' && !keyP) return html
  const key = keyP ? await keyP : null
  // The image's view: the page's, plus `/staged`'s plan version and, for a
  // full card, the credential it re-checks.
  const v = pv.kind === 'staged' ? await stagedVersion(env) : null
  const scan = pv.kind === 'staged' ? null : await resolveScan(env, pv.params.d)
  const params = { ...imageParams(pv.params, v, cred), ...(scan ? { s: scan } : {}), policy: anonymousPolicy(env), r: RENDER_VERSION }
  const image = url.origin + await imagePath(key, pv.kind, params, tier, day)
  if (OWN_TITLES.has(pv.kind)) return uncachedHtml(stampMeta(html, { image, imageType: 'image/png', page: url.href }))
  return uncachedHtml(stampMeta(html, {
    title: pv.title,
    desc: env.PUBLIC_READ
      ? `${siteOf(env).name}: explore storage by size and path; browse folders and compare scans.`
      : `${siteOf(env).name}: storage snapshot preview. Comparison links preview the after snapshot, not the diff.`,
    image,
    imageType: 'image/png',
    page: url.href,
  }))
}

let fontsMemo: Promise<Uint8Array[]> | null = null
function fonts(env: OgEnv, origin: string): Promise<Uint8Array[]> {
  fontsMemo ??= Promise.all(FONT_FILES.map(async f => {
    const r = await env.ASSETS.fetch(new Request(origin + f))
    if (!r.ok) throw new Error(`font ${f}: ${r.status}`)
    return new Uint8Array(await r.arrayBuffer())
  })).catch(e => { fontsMemo = null; throw e })
  return fontsMemo
}

async function cardData(env: OgEnv, kind: string, params: Record<string, string>, tier: OgTier, url: URL): Promise<CardData | null> {
  const site = siteOf(env)
  // The page's own title, recomputed from the signed view.
  const page = kind === 'map' ? `/${params.path ?? ''}` : kind === 'user' ? `/user/${params.id ?? ''}` : `/${kind}`
  const pu = new URL(page, url.origin)
  for (const [k, v] of Object.entries(params)) if (k !== 'path' && k !== 'id') pu.searchParams.set(k, v)
  const title = pageView(pu, site.name)?.title ?? site.name
  switch (kind) {
    case 'map': return mapCard(env, site, title, params, tier)
    case 'staged': return stagedCard(env, site, title, params, tier)
    case 'users': return usersCard(env, site, title, params, tier)
    case 'user': return userCard(env, site, title, params, tier)
    case 'assignments': return assignmentsCard(env, site, title, params, tier)
    default: return null
  }
}

/** Fill the edge cache with the card view's first-paint reads (`warm.ts`):
 * at most two subtree requests, no retries, errors swallowed. */
async function warmView(env: OgEnv, origin: string, kind: string, params: Record<string, string>, waitUntil?: (p: Promise<unknown>) => void): Promise<void> {
  try {
    const date = await resolveScan(env, params.d)
    if (!date) return
    const reg = await loadRegistry(env).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>)
    for (const u of warmUrls(kind, params, date, x => canonId(x, reg))) await warmSubtree(env, origin + u, waitUntil).catch(() => null)
  } catch {
    // Best-effort: a cold clickthrough is the status quo.
  }
}

/** `GET /og/<kind>.png?<view>[&t=|&g=…&sig=…]`: the full card only for a
 * valid `sig` whose credential (`t` token row / `g` grant) is still live,
 * checked on every fetch, cache hit or not; the anonymous card otherwise
 * (never an error). Rendered or served from the colo cache, keyed by tier +
 * view (+ credential) + the ledger head (an assignment recolours a card
 * within the URL's lifetime). */
export async function serveCard(ctx: { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> {
  const { request, env } = ctx
  const url = new URL(request.url)
  const keyP = deploymentKey(env)
  const t = now()
  const r0 = await resolveImage(keyP ? await keyP : null, url, t)
  if (!r0 || !DRAWN.has(r0.kind as OgKind)) return new Response('not a card', { status: 404 })
  const { view, v, scan: pinnedScan, cred } = splitImageParams(r0.params)
  const r = r0.tier === 'full' && !(await credLive(env, r0.kind, view, cred, t))
    ? { ...r0, tier: 'anon' as const, why: cred ? 'revoked or expired' : 'no credential' }
    : r0
  const policy = anonymousPolicy(env)
  if (r.tier === 'anon' && policy === 'generic') {
    // No ledger/scan/owner/data reads or warming, and never an old view cache key.
    // Even invalid/revoked credentials return identical content and tier headers.
    const cacheKey = new Request(url.origin + genericImagePath)
    const cache = caches.default
    const hit = await cache.match(cacheKey)
    const response = hit ?? await renderCard(env, url, genericCard(env))
    if (!hit) await cache.put(cacheKey, response.clone())
    const headers = new Headers(response.headers)
    headers.set('x-og-tier', 'anon')
    headers.set('x-og-policy', policy)
    headers.set('cache-control', url.searchParams.get('r') === RENDER_VERSION && url.searchParams.get('policy') === policy ? 'public, max-age=86400' : 'no-store')
    return new Response(response.body, { headers })
  }
  let scan: string | null
  try {
    scan = r.kind === 'staged' ? null : pinnedScan ?? await resolveScan(env, view.d)
  } catch (e) {
    if (e instanceof NoScanMatch) return json({ error: e.message }, 404)
    throw e
  }
  const renderView = scan ? { ...view, d: scan } : view
  const head = env.DB ? await ledgerHead(env).catch(() => 0) : 0
  // An anonymous render is one per view (+ version), whatever credential the URL named.
  const keyParams = { ...(r.tier === 'full' ? r0.params : { ...view, ...(v ? { v } : {}) }), ...(scan ? { s: scan } : {}), policy, r: RENDER_VERSION }
  const cacheKey = new Request(`${url.origin}${await imagePath(null, r.kind, keyParams, 'anon')}${Object.keys(keyParams).length ? '&' : '?'}tier=${r.tier}&h=${head}`)
  const cache = caches.default
  if (ctx.waitUntil) ctx.waitUntil(warmView(env, url.origin, r.kind, renderView, p => ctx.waitUntil!(p)))
  const hit = await cache.match(cacheKey)
  if (hit) return withTier(hit, r.tier, r.why, !!pinnedScan || !!v)
  const t0 = Date.now()
  let data: CardData | null
  try {
    data = await cardData(env, r.kind, renderView, r.tier, url)
  } catch (e) {
    // `?d=` names no scan: a 404, never a card of another scan.
    if (e instanceof NoScanMatch) return json({ error: e.message }, 404)
    throw e
  }
  if (!data) return new Response('no such card', { status: 404 })
  const t1 = Date.now()
  const res = await renderCard(env, url, data)
  res.headers.set('server-timing', `data;dur=${t1 - t0}, render;dur=${Date.now() - t1}`)
  const put = cache.put(cacheKey, new Response(res.clone().body, { headers: { ...Object.fromEntries(res.headers), 'cache-control': 'public, max-age=86400' } }))
  if (ctx.waitUntil) ctx.waitUntil(put)
  else await put
  return withTier(res, r.tier, r.why, !!pinnedScan || !!v)
}

async function renderCard(env: OgEnv, url: URL, data: CardData): Promise<Response> {
  await ensureWasm(RESVG)
  return new Response(svgToPng(cardSvg(data), await fonts(env, url.origin)), {
    headers: { 'content-type': 'image/png', 'cache-control': 'public, max-age=86400' },
  })
}

/** The response with this fetch's tier header (and the cache policy its tier needs). */
function withTier(res: Response, tier: OgTier, why?: string, pinned = false): Response {
  const h = new Headers(res.headers)
  h.set('x-og-tier', why ? `anon (${why})` : tier)
  h.set('cache-control', tier === 'full' ? 'private, no-store' : pinned ? 'public, max-age=86400' : 'public, max-age=0, must-revalidate')
  return new Response(res.body, { status: res.status, headers: h })
}
