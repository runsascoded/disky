/**
 * `GET /og/fleet.png[?theme=light][&v=…][&sig=…]`: the fleet card (`_lib/card.ts`). With a valid `sig` (HMAC under
 * `SESSION_SECRET`, `IMAGE_TTL_DAYS`: `@disk-tree/pages-kit/og/sign`, as `site/`'s cards) it is the full card, drawn
 * from the cached fleet document; without one, or with a bad or expired one, the anonymous card (names and colours,
 * no numbers), never an error; with `FLEET_CARD_PUBLIC = "1"` every request gets the full card, cached briefly. `v` is
 * only a cache-buster (the Slack post's, a new one each run). `view` (signed with the rest) draws one of the post's
 * reply images instead (`_lib/replies.ts` `viewSvg`: `month`, `timeline`, `cost`, `deploy` with `d=<deployment>`); an
 * unknown view or deployment is a 404, and an anonymous request for any view gets the anonymous card.
 */
import { ogKey, resolveImage } from '@disk-tree/pages-kit/og/sign'
import { fleetSvg } from '../_lib/card.js'
import { viewSvg, viewZoom } from '../_lib/replies.js'
import type { Ctx } from '../_lib/env.js'
import { type Deployment, fleetDoc, parseFleet } from '../_lib/fleet.js'
import { renderPng } from '../_lib/png.js'

const deploymentsOf = (raw: string | undefined): Deployment[] => {
  try {
    return parseFleet(raw)
  } catch (e) {
    console.log(`og/fleet.png: ${e instanceof Error ? e.message : e}`)
    return []
  }
}

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const now = Math.floor(Date.now() / 1000)
  const key = ctx.env.SESSION_SECRET ? await ogKey(ctx.env.SESSION_SECRET) : null
  const r = await resolveImage(key, url, now)
  if (!r || r.kind !== 'fleet') return new Response('no such card\n', { status: 404 })
  const deployments = deploymentsOf(ctx.env.FLEET_JSON).map(({ name, color }) => ({ name, color }))
  const open = ctx.env.FLEET_CARD_PUBLIC === '1'
  const tier = r.tier === 'full' || open ? 'full' : 'anon'
  const doc = tier === 'full' ? await fleetDoc(ctx.env, { waitUntil: ctx.waitUntil }) : undefined
  const theme = r.params.theme === 'light' ? 'light' : 'dark'
  const svg = doc ? viewSvg(r.params.view ?? null, r.params.d ?? null, { theme, doc, deployments }) : fleetSvg({ tier, theme, deployments, now })
  if (svg === null) return new Response('no such view\n', { status: 404 })
  const png = await renderPng(ctx.env, url.origin, svg, doc ? viewZoom(r.params.view ?? null) : undefined)
  return new Response(png, {
    headers: {
      'content-type': 'image/png',
      // a public card is the fleet document's (held 5 min at the edge): a link preview may keep it that long
      'cache-control': open ? 'public, max-age=300' : tier === 'full' ? 'private, no-store' : 'public, max-age=3600',
      'x-og-tier': open ? 'full (public)' : r.why ? `anon (${r.why})` : r.tier,
    },
  })
}
