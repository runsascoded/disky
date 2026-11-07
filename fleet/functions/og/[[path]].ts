/**
 * `GET /og/fleet.png[?theme=light][&v=…][&sig=…]`: the fleet card (`_lib/card.ts`). With a valid `sig` (HMAC under
 * `SESSION_SECRET`, `IMAGE_TTL_DAYS`: `@disk-tree/pages-kit/og/sign`, as `site/`'s cards) it is the full card, drawn
 * from the cached fleet document; without one, or with a bad or expired one, the anonymous card (names and colours,
 * no numbers), never an error. `v` is only a cache-buster (the Slack post's, a new one each run).
 */
import { ogKey, resolveImage } from '@disk-tree/pages-kit/og/sign'
import { fleetSvg } from '../_lib/card.js'
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
  const doc = r.tier === 'full' ? await fleetDoc(ctx.env, { waitUntil: ctx.waitUntil }) : undefined
  const svg = fleetSvg({ tier: r.tier, theme: r.params.theme === 'light' ? 'light' : 'dark', deployments, doc, now: doc?.now ?? now })
  const png = await renderPng(ctx.env, url.origin, svg)
  return new Response(png, {
    headers: {
      'content-type': 'image/png',
      'cache-control': r.tier === 'full' ? 'private, no-store' : 'public, max-age=3600',
      'x-og-tier': r.why ? `anon (${r.why})` : r.tier,
    },
  })
}
