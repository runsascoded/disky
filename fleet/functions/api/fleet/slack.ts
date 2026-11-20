/**
 * `POST /api/fleet/slack[?dry=1][&new=1]` (Bearer `FLEET_JOB_TOKEN`, the hourly cron's): read the fleet anew, build the
 * post (`_lib/slack.ts`), draw its card (`_lib/card.ts`, as `/og/fleet.png` draws the full one), and post it once with
 * the card attached, then edit it in place, swapping the card. `dry` answers the message and the card's size without
 * posting; `new` posts a new message in place of the kept one. Answers `{ action, channel, ts, file, undeleted }`.
 */
import { fleetSvg } from '../../_lib/card.js'
import { type Ctx, json, same } from '../../_lib/env.js'
import { fleetDoc } from '../../_lib/fleet.js'
import { renderPng } from '../../_lib/png.js'
import { fleetMessage, publish } from '../../_lib/slack.js'

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const { env } = ctx
  const expected = env.FLEET_JOB_TOKEN
  if (!expected) return json({ error: 'FLEET_JOB_TOKEN is not set' }, 503)
  const presented = /^Bearer (.+)$/.exec(ctx.request.headers.get('authorization') ?? '')?.[1] ?? ''
  if (!same(presented, expected)) return json({ error: 'unauthenticated' }, 401)
  const unset = (['DISKY_SLACK_BOT_TOKEN', 'SLACK_FLEET_CHANNEL'] as const).filter(k => !env[k])
  const url = new URL(ctx.request.url)
  const dry = url.searchParams.has('dry')
  if (unset.length && !dry) return json({ error: `not configured: ${unset.join(', ')}` }, 503)
  const doc = await fleetDoc(env, { fresh: true, waitUntil: ctx.waitUntil })
  const msg = fleetMessage(doc, `${url.origin}/fleet`)
  const deployments = doc.deployments.map(({ name, color }) => ({ name, color }))
  const card = await renderPng(env, url.origin, fleetSvg({ tier: 'full', theme: 'dark', deployments, doc, now: doc.now }))
  if (dry) return json({ action: 'dry', card_bytes: card.byteLength, ...msg })
  try {
    return json(await publish(env, msg, card, url.searchParams.has('new')))
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 502)
  }
}
