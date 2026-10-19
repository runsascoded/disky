/** The edge half of the cards (specs/done/dogi.md): the deployment's OG config,
 * page meta stamping, and `/og/<kind>.png` rendering with the colo cache. */
import RESVG from './vendor/resvg.wasm'
import { type Env, json } from '../auth.js'
import { stampMeta } from '../unfurl.js'
import { cardSvg, type CardData } from './card.js'
import { mapCard, type Site } from './data.js'
import { stagedImageVersion } from '../stagedSlack.js'
import { stagedCard } from './pages.js'
import { ensureWasm, FONT_FILES, svgToPng } from './render.js'
import { KIND_SECTION, pageView, storeOfPath, type OgKind, type PageView } from './routes.js'
import { imagePath, ogKey, resolveImage, type OgTier } from './sign.js'
import { credLive, pageTier } from './tier.js'
import { imageParams, splitImageParams } from './cred.js'
import { warmUrls } from './warm.js'
import { anonymousPolicy, cardTier, genericCard, genericImagePath, genericMeta, RENDER_VERSION, type PolicyEnv } from './policy.js'
import { BUILD } from '../build.js'

import { canonId, loadRegistry } from '../identity.js'
import { Memo, reqOf, withReq } from '../shared.js'
import { withPathStore } from '../index.js'
import { isPrimary, secondaryStores, StoreConfigError, storeEnv, storePaths } from '../stores.js'
import { openPlanId, planDigest, planItemLines } from '../plans.js'
import { NoScanMatch, resolveScan } from './data.js'
import { warmSubtree } from '../../api/subtree.js'
import { colorModeCodec } from '../../../src/colorMode.js'
import { diffCard, resolveDiffScans } from './diff.js'
import { seriesCard } from './series.js'
import { encodeScan } from '../../../src/scanSlug.js'
import { SECTION_KEY } from '../../../src/homeSections.js'
import type { ColorMode } from '../../../src/types.js'

export type OgEnv = Env & PolicyEnv & {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  /** Stamp detailed/shape HTML metadata. Generic private metadata remains safe
   * even when unset; this does not disable direct image routes. */
  OG_CARDS?: string
  /** Match the frontend Store.defaultColor when configured; otherwise infer
   * owner vs tree from the scan's attribution, as the dashboard does. */
  OG_DEFAULT_COLOR?: string
  /** The store's mount (`Store.root`), `/`-separated: the segments its page URLs leave out. */
  STORE_ROOT?: string
}

const uncachedHtml = (response: Response): Response => {
  const headers = new Headers(response.headers)
  headers.set('cache-control', 'private, no-store')
  return new Response(response.body, { status: response.status, headers })
}

/** `env` as secondary store `key`'s card sees it (`stores.ts` `storeEnv`: its label, scheme, mount, data and
 * scope), or null for no such store. */
function storeOgEnv(env: OgEnv, key: string): OgEnv | null {
  const stores = secondaryStores(env)
  return Object.hasOwn(stores, key) ? storeEnv(env, key, stores[key]) as OgEnv : null
}

/** A page URL's store and view: below a secondary store's `path` (`STORES_JSON`), that store's env, site and view
 * (its params carrying `store`); else the primary's, as ever. A `STORES_JSON` that doesn't parse leaves every page
 * the primary's (the data Functions answer its 500). */
export function pageOf(env: OgEnv, url: URL): { env: OgEnv; site: Site; pv: PageView } | null {
  let stores: { key: string; path: string }[] = []
  try {
    stores = storePaths(env)
  } catch (e) {
    if (!(e instanceof StoreConfigError)) throw e
  }
  const at = storeOfPath(url.pathname, stores)
  const senv = at ? storeOgEnv(env, at.key)! : env
  const site = siteOf(senv)
  const pv = at ? pageView(new URL(at.rest + url.search + url.hash, url.origin), site.name, site.root, at.key) : pageView(url, site.name, site.root)
  return pv && { env: senv, site, pv }
}

/** The env a card's data reads go through: the data Functions' path-store rule (`withPathStore`), as a request for the
 * store would be. The primary's under `PATH_STORE=intervals` reads the interval store at its pinned or current
 * generation (`CURRENT.json` when unpinned), as the page's own reads do: a scan the store holds without per-scan
 * pointers (a lean scan) draws, never "view unavailable". A secondary store's as a `store=` read (the interval store
 * is the primary's). */
async function dataEnv(env: OgEnv, origin: string): Promise<OgEnv> {
  const q = isPrimary(env) ? '' : `?store=${encodeURIComponent(env.STORE_KEY!)}`
  return (await withPathStore({ request: new Request(`${origin}/${q}`), env })).env as OgEnv
}

/** Called before route Functions: private anonymous unfurls must not read
 * staging/owner/scan data even to build their HTML descriptions. Only explicit
 * preview/share credentials are checked; browser sessions do not grant previews. */
export async function anonymousPage(env0: OgEnv, url: URL): Promise<Response | null> {
  const page = pageOf(env0, url)
  if (!page || !DRAWN.has(page.pv.kind)) return null
  const { env, pv } = page
  if (anonymousPolicy(env) !== 'generic') return null
  if ((await pageTier(env, pv.kind, pv.params, url, now())).tier === 'full') return null
  const shell = await env.ASSETS.fetch(new Request(new URL('/', url)))
  return uncachedHtml(stampMeta(shell, genericMeta(env, url)))
}

/** The kinds this build draws; the rest keep their static card. */
export const DRAWN: ReadonlySet<OgKind> = new Set(['map', 'diff', 'series', 'staged'])

/** Kinds whose page Function already stamps its own title and description
 * (`functions/staged.ts`): the middleware only swaps their image. */
const OWN_TITLES: ReadonlySet<OgKind> = new Set(['staged'])

export const siteOf = (env: OgEnv): Site => {
  const c = colorModeCodec.decode(env.OG_DEFAULT_COLOR)
  return {
    name: env.ROOT_LABEL ?? env.STORE_KEY ?? 'storage', scheme: env.STORE_SCHEME ?? 'gs://',
    ...(env.STORE_ROOT ? { root: env.STORE_ROOT.split('/').filter(Boolean) } : {}),
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
export async function stampPage(env0: OgEnv, url: URL, html: Response, warm?: (p: Promise<unknown>) => void): Promise<Response> {
  const page = pageOf(env0, url)
  if (!page || !DRAWN.has(page.pv.kind)) return html
  const { pv, site } = page
  const env = await dataEnv(page.env, url.origin)
  const { tier, day, cred } = await pageTier(env, pv.kind, pv.params, url, now())
  if (tier === 'anon' && anonymousPolicy(env) === 'generic') return uncachedHtml(stampMeta(html, genericMeta(env, url)))
  if (!env.OG_CARDS) return html
  const keyP = deploymentKey(env)
  if (tier === 'full' && !keyP) return html
  const key = keyP ? await keyP : null
  // The image's view: the page's, plus `/staged`'s plan version and, for a
  // full card, the credential it re-checks.
  const v = pv.kind === 'staged' ? await stagedVersion(env) : null
  const pair = pv.kind === 'diff' ? await resolveDiffScans(env, pv.params.d) : null
  const scan = pv.kind === 'diff' ? pair?.to : pv.kind === 'staged' ? null : await resolveScan(env, pv.params.d)
  const params = { ...imageParams(pv.params, v, cred), ...(scan ? { s: scan } : {}), ...(pair?.from ? { b: pair.from } : {}), policy: anonymousPolicy(env), r: RENDER_VERSION }
  const image = url.origin + await imagePath(key, pv.kind, params, tier, day)
  // A full card's image rendered while the unfurler reads the page: it fetches the image next, from the same colo, and a
  // cold full render could outlast its wait (Slack showed a title and no image, 2026-10-19)
  if (warm && tier === 'full') warm(fetch(image).then(r => r.arrayBuffer()).catch(() => undefined))
  if (pv.kind === 'diff') {
    const render = await diffCard(env, site, pv.title, { ...pv.params, ...(pair?.from ? { d: `${encodeScan(pair.to)}-${encodeScan(pair.from)}` } : {}) }, cardTier(tier, anonymousPolicy(env)), true)
    return uncachedHtml(stampMeta(html, { title: render.title, desc: `${render.subtitle} · ${render.total}`, image, imageType: 'image/png', page: url.href }))
  }
  if (pv.kind === 'series') {
    // the series' span and latest size in the preview text, as the diff's comparison
    const render = await seriesCard(env, site, pv.title, { ...pv.params, ...(scan ? { d: scan } : {}) }, cardTier(tier, anonymousPolicy(env)), url.origin)
    return uncachedHtml(stampMeta(html, { title: render.title, desc: [render.subtitle, render.total].filter(Boolean).join(' · ') || render.empty || pv.title, image, imageType: 'image/png', page: url.href }))
  }
  if (OWN_TITLES.has(pv.kind)) return uncachedHtml(stampMeta(html, { image, imageType: 'image/png', page: url.href }))
  if (cardTier(tier, anonymousPolicy(env)) === 'full' && pv.kind === 'map') {
    // the full card's own numbers in the preview text (`scan … · 263 TiB · 1.2M objects`)
    // Only its totals are stamped: the floor view, no deepening on the page's response.
    const render = await mapCard(env, site, pv.title, { ...pv.params, ...(scan ? { d: scan } : {}) }, 'full', { deepen: false })
    return uncachedHtml(stampMeta(html, { title: pv.title, desc: [render.subtitle, render.total].filter(Boolean).join(' · '), image, imageType: 'image/png', page: url.href }))
  }
  return uncachedHtml(stampMeta(html, {
    title: pv.title,
    desc: env.PUBLIC_READ
      ? `${site.name}: explore storage by size and path; browse folders and compare scans.`
      : `${site.name}: storage snapshot preview. Comparison links preview the after snapshot, not the diff.`,
    image,
    imageType: 'image/png',
    page: url.href,
  }))
}

/** The card fonts, held per isolate once fetched; a fetch pending in another request is joined (`shared.ts`). */
const fontsMemo = new Memo<Uint8Array[]>('og-fonts')
export function fonts(env: OgEnv, origin: string): Promise<Uint8Array[]> {
  return fontsMemo.get('', () => Promise.all(FONT_FILES.map(async f => {
    const r = await env.ASSETS.fetch(new Request(origin + f))
    if (!r.ok) throw new Error(`font ${f}: ${r.status}`)
    return new Uint8Array(await r.arrayBuffer())
  })), reqOf(env))
}

/** `env`: the card's store's (`storeOgEnv`), its data reads' (`dataEnv`). */
async function cardData(env: OgEnv, kind: string, params: Record<string, string>, tier: OgTier, url: URL): Promise<CardData | null> {
  const site = siteOf(env)
  // The page's own title, recomputed from the signed view (a secondary store's below its path).
  const page = kind === 'map' || Object.hasOwn(KIND_SECTION, kind) ? `/${params.path ?? ''}` : kind === 'user' ? `/user/${params.id ?? ''}` : `/${kind}`
  const pu = new URL(page, url.origin)
  const section = KIND_SECTION[kind as OgKind]
  if (section) pu.searchParams.set(SECTION_KEY, section)
  for (const [k, v] of Object.entries(params)) if (k !== 'path' && k !== 'id' && k !== 'store') pu.searchParams.set(k, v)
  const title = pageView(pu, site.name, site.root, params.store)?.title ?? site.name
  switch (kind) {
    case 'map': return mapCard(env, site, title, params, tier)
    case 'diff': return diffCard(env, site, title, params, tier)
    case 'series': return seriesCard(env, site, title, params, tier, url.origin)
    case 'staged': return stagedCard(env, site, title, params, tier)
    default: return null
  }
}

/** Fill the edge cache with the card view's first-paint reads (`warm.ts`):
 * at most two subtree requests, no retries, errors swallowed. `env` is the deployment's (a warm read's `store=`
 * resolves its store, as the page's own would), `sEnv` the card's store's. */
async function warmView(env: OgEnv, sEnv: OgEnv, origin: string, kind: string, params: Record<string, string>, waitUntil?: (p: Promise<unknown>) => void): Promise<void> {
  try {
    const date = await resolveScan(sEnv, params.d)
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
  const { request } = ctx
  // The env carries the request (`withReq`): its shared reads are owned by it, and its cancel evicts them.
  const env = withReq(ctx.env, request.signal)
  const url = new URL(request.url)
  const keyP = deploymentKey(env)
  const t = now()
  const r0 = await resolveImage(keyP ? await keyP : null, url, t)
  if (!r0 || !DRAWN.has(r0.kind as OgKind)) return new Response('not a card', { status: 404 })
  const { view, v, scan: pinnedScan, before: pinnedBefore, cred } = splitImageParams(r0.params)
  // A secondary store's card (`store=`, map, diff and series only): its label, mount, scope and data.
  let sEnv: OgEnv = await dataEnv(env, url.origin)
  if ('store' in view) {
    const o = r0.kind === 'map' || Object.hasOwn(KIND_SECTION, r0.kind) ? storeOgEnv(env, view.store) : null
    if (!o) return new Response('not a card', { status: 404 })
    sEnv = await dataEnv(o, url.origin)
  }
  const r = r0.tier === 'full' && !(await credLive(sEnv, r0.kind, view, cred, t))
    ? { ...r0, tier: 'anon' as const, why: cred ? 'revoked or expired' : 'no credential' }
    : r0
  const policy = anonymousPolicy(sEnv)
  if (r.tier === 'anon' && policy === 'generic') {
    // No ledger/scan/owner/data reads or warming, and never an old view cache key.
    // Even invalid/revoked credentials return identical content and tier headers.
    const cacheKey = new Request(`${url.origin}${genericImagePath}&bld=${BUILD.sha}`)
    const cache = caches.default
    const hit = await cache.match(cacheKey)
    const response = hit ?? await renderCard(env, url, genericCard(env))
    if (!hit) await cache.put(cacheKey, stored(response.clone()))
    const headers = new Headers(response.headers)
    headers.set('x-og-tier', 'anon')
    headers.set('x-og-policy', policy)
    headers.set('cache-control', url.searchParams.get('r') === RENDER_VERSION && url.searchParams.get('policy') === policy ? 'public, max-age=86400' : 'no-store')
    return new Response(response.body, { headers })
  }
  let scan: string | null
  let before = pinnedBefore
  try {
    const pair = r.kind === 'diff' && !pinnedScan ? await resolveDiffScans(sEnv, view.d) : null
    scan = r.kind === 'diff' ? pinnedScan ?? pair?.to ?? null : r.kind === 'staged' ? null : pinnedScan ?? await resolveScan(sEnv, view.d)
    before ??= pair?.from ?? undefined
  } catch (e) {
    if (e instanceof NoScanMatch) return json({ error: e.message }, 404)
    throw e
  }
  const renderView = scan ? { ...view, d: r.kind === 'diff' && before ? `${encodeScan(scan)}-${encodeScan(before)}` : scan } : view
  const head = 0  // m3 keeps no ownership ledger
  // An anonymous render is one per view (+ version), whatever credential the URL named.
  const keyParams = { ...(r.tier === 'full' ? r0.params : { ...view, ...(v ? { v } : {}) }), ...(scan ? { s: scan } : {}), ...(before ? { b: before } : {}), policy, r: RENDER_VERSION }
  const cacheKey = new Request(`${url.origin}${await imagePath(null, r.kind, keyParams, 'anon')}${Object.keys(keyParams).length ? '&' : '?'}tier=${r.tier}&h=${head}&bld=${BUILD.sha}`)
  const cache = caches.default
  if (ctx.waitUntil) ctx.waitUntil(warmView(env, sEnv, url.origin, r.kind, renderView, p => ctx.waitUntil!(p)))
  const hit = await cache.match(cacheKey)
  if (hit) return withTier(hit, r.tier, r.why, !!pinnedScan || !!v)
  const t0 = Date.now()
  let data: CardData | null
  try {
    data = await cardData(sEnv, r.kind, renderView, cardTier(r.tier, policy), url)
  } catch (e) {
    // `?d=` names no scan: a 404, never a card of another scan.
    if (e instanceof NoScanMatch) return json({ error: e.message }, 404)
    throw e
  }
  if (!data) return new Response('no such card', { status: 404 })
  const t1 = Date.now()
  const res = await renderCard(env, url, data)
  res.headers.set('server-timing', `data;dur=${t1 - t0}${data.depth ? `;desc="depth ${data.depth}"` : ''}, render;dur=${Date.now() - t1}`)
  if (data.failed) {
    // A read error drawn as "view unavailable": no cache anywhere (the colo's, the edge's, Slack's), so the next fetch
    // retries rather than serving it for the URL's lifetime.
    const out = withTier(res, r.tier, r.why)
    out.headers.set('cache-control', 'no-store')
    return out
  }
  const put = cache.put(cacheKey, stored(res.clone()))
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

/** A rendered card as the colo cache holds it: a year. Its key names everything the image depends on (the view, the
 *  scan, the render version, the ledger head, and the build, whose config — labels, colours, policy — a deploy can
 *  change without a new `RENDER_VERSION`), so the entry never goes stale; a new scan or deploy is a new key. What a
 *  client caches (`withTier`, the generic path) stays a day: its URL names the scan and version, not the ledger or
 *  the build. */
function stored(res: Response): Response {
  return new Response(res.body, { headers: { ...Object.fromEntries(res.headers), 'cache-control': 'public, max-age=31536000' } })
}

/** The response with this fetch's tier header (and the cache policy its tier needs). */
function withTier(res: Response, tier: OgTier, why?: string, pinned = false): Response {
  const h = new Headers(res.headers)
  h.set('x-og-tier', why ? `anon (${why})` : tier)
  h.set('cache-control', tier === 'full' ? 'private, no-store' : pinned ? 'public, max-age=86400' : 'public, max-age=0, must-revalidate')
  return new Response(res.body, { status: res.status, headers: h })
}
