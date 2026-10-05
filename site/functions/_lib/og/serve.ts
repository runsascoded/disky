/** The edge half of the cards (specs/done/dogi.md): the deployment's OG config,
 * page meta stamping, and `/og/<kind>.png` rendering with the colo cache. */
import RESVG from './vendor/resvg.wasm'
import type { Env } from '../auth.js'
import { ledgerHead } from '../ledger.js'
import { stampMeta } from '../unfurl.js'
import { cardSvg, type CardData } from './card.js'
import { mapCard, type Site } from './data.js'
import { assignmentsCard, stagedCard, userCard, usersCard } from './pages.js'
import { ensureWasm, FONT_FILES, svgToPng } from './render.js'
import { pageView, type OgKind } from './routes.js'
import { expDay, IMAGE_TTL_DAYS, imagePath, ogKey, resolveImage, type OgTier } from './sign.js'
import { credLive, pageTier } from './tier.js'
import { imageParams, splitImageParams } from './cred.js'
import { warmUrls } from './warm.js'

import { canonId, loadRegistry } from '../identity.js'
import { openPlanId, planDigest } from '../plans.js'
import { resolveScan } from './data.js'
import { warmSubtree } from '../../api/subtree.js'

export type OgEnv = Env & {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  /** Set (any value) to stamp dynamic cards; unset = the static `og.jpg`. */
  OG_CARDS?: string
}

/** The kinds this build draws; the rest keep their static card. */
export const DRAWN: ReadonlySet<OgKind> = new Set(['map', 'staged', 'users', 'user', 'assignments'])

/** Kinds whose page Function already stamps its own title and description
 * (`functions/staged.ts`, `users.ts`, `user/[id].ts`, `assignments.ts`): the
 * middleware only swaps their image. */
const OWN_TITLES: ReadonlySet<OgKind> = new Set(['staged', 'users', 'user', 'assignments'])

export const siteOf = (env: OgEnv): Site => ({ name: env.ROOT_LABEL ?? 'storage', scheme: env.STORE_SCHEME ?? 'gs://' })

let keyMemo: { secret: string; key: Promise<CryptoKey> } | null = null
/** The OG key for this deployment, or null when it has no `SESSION_SECRET`. */
export function deploymentKey(env: OgEnv): Promise<CryptoKey> | null {
  const s = env.SESSION_SECRET
  if (!s) return null
  if (keyMemo?.secret !== s) keyMemo = { secret: s, key: ogKey(s) }
  return keyMemo.key
}

const now = () => Math.floor(Date.now() / 1000)

/** The open plan's digest (first 8 hex), so `/staged`'s image URL changes
 * with every staging gesture and Slack / the colo cache never serve a stale
 * card. Null when there's no plan. */
async function stagedVersion(env: OgEnv): Promise<string | null> {
  if (!env.DB) return null
  const plan = await openPlanId(env.DB).catch(() => null)
  if (plan == null) return null
  const items = (await env.DB.prepare('SELECT prefix FROM plan_items WHERE plan_id = ?').bind(plan).all<{ prefix: string }>()).results
  return (await planDigest(items.map(i => i.prefix))).slice(0, 8)
}

/** Stamp a page's HTML with its card when it has one. */
export async function stampPage(env: OgEnv, url: URL, html: Response): Promise<Response> {
  if (!env.OG_CARDS) return html
  const pv = pageView(url, siteOf(env).name)
  if (!pv || !DRAWN.has(pv.kind)) return html
  const keyP = deploymentKey(env)
  if (!keyP) return html
  const key = await keyP
  const { tier, day, cred } = await pageTier(env, pv.kind, pv.params, url, now())
  // The image's view: the page's, plus `/staged`'s plan version and, for a
  // full card, the credential it re-checks.
  const v = pv.kind === 'staged' ? await stagedVersion(env) : null
  const image = url.origin + await imagePath(key, pv.kind, imageParams(pv.params, v, cred), tier, day)
  if (OWN_TITLES.has(pv.kind)) return stampMeta(html, { image, imageType: 'image/png', page: url.href })
  return stampMeta(html, {
    title: pv.title,
    desc: `${siteOf(env).name}: an interactive storage map of this view: who owns what, by size.`,
    image,
    imageType: 'image/png',
    page: url.href,
  })
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
  if (!r0) return new Response('not a card', { status: 404 })
  const { view, v, cred } = splitImageParams(r0.params)
  const r = r0.tier === 'full' && !(await credLive(env, r0.kind, view, cred, t))
    ? { ...r0, tier: 'anon' as const, why: cred ? 'revoked or expired' : 'no credential' }
    : r0
  const head = env.DB ? await ledgerHead(env).catch(() => 0) : 0
  // An anonymous render is one per view (+ version), whatever credential the URL named.
  const keyParams = r.tier === 'full' ? r0.params : { ...view, ...(v ? { v } : {}) }
  const cacheKey = new Request(`${url.origin}${await imagePath(null, r.kind, keyParams, 'anon')}${Object.keys(keyParams).length ? '&' : '?'}tier=${r.tier}&h=${head}`)
  const cache = (caches as unknown as { default: Cache }).default
  if (ctx.waitUntil) ctx.waitUntil(warmView(env, url.origin, r.kind, view, ctx.waitUntil))
  const hit = await cache.match(cacheKey)
  if (hit) return withTier(hit, r.tier, r.why)
  const t0 = Date.now()
  const data = await cardData(env, r.kind, view, r.tier, url)
  if (!data) return new Response('no such card', { status: 404 })
  const t1 = Date.now()
  await ensureWasm(RESVG)
  const png = svgToPng(cardSvg(data), await fonts(env, url.origin))
  const t2 = Date.now()
  const res = new Response(png, {
    headers: {
      'content-type': 'image/png',
      // Anonymous cards show no names: a day's caching anywhere is fine. A
      // full card's revocation must reach the next fetch, so only the colo
      // cache (keyed here, behind the credential check) keeps it.
      'cache-control': r.tier === 'full' ? 'private, no-store' : 'public, max-age=86400',
      'server-timing': `data;dur=${t1 - t0}, render;dur=${t2 - t1}`,
    },
  })
  const put = cache.put(cacheKey, new Response(res.clone().body, { headers: { ...Object.fromEntries(res.headers), 'cache-control': 'public, max-age=86400' } }))
  if (ctx.waitUntil) ctx.waitUntil(put)
  else await put
  return withTier(res, r.tier, r.why)
}

/** The response with this fetch's tier header (and the cache policy its tier needs). */
function withTier(res: Response, tier: OgTier, why?: string): Response {
  const h = new Headers(res.headers)
  h.set('x-og-tier', why ? `anon (${why})` : tier)
  h.set('cache-control', tier === 'full' ? 'private, no-store' : 'public, max-age=86400')
  return new Response(res.body, { status: res.status, headers: h })
}
