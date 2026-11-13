/**
 * `GET /api/fleet[?fresh]` → the fleet document (`_lib/fleet.ts`) and the full card's URLs (`card.dark`, `card.light`),
 * for a staff session. The document is read at most every `FLEET_TTL` s per colo (`fresh` reads anew); the browser
 * never keeps it.
 */
import { cardUrl } from '../../_lib/cardUrl.js'
import { type Ctx, json, requireStaff } from '../../_lib/env.js'
import { fleetDoc } from '../../_lib/fleet.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const who = await requireStaff(ctx)
  if (who instanceof Response) return who
  const url = new URL(ctx.request.url)
  const doc = await fleetDoc(ctx.env, { fresh: url.searchParams.has('fresh'), waitUntil: ctx.waitUntil })
  const secret = ctx.env.SESSION_SECRET
  const now = Math.floor(Date.now() / 1000)
  const v = String(doc.now)
  // No secret (local dev's `DEV_EMAIL` without one): nothing to sign with, so no card.
  const card = secret ? { dark: await cardUrl(secret, url.origin, now, { v }), light: await cardUrl(secret, url.origin, now, { v, theme: 'light' }) } : null
  return json({ ...doc, card }, 200, { 'cache-control': 'private, no-store' })
}
