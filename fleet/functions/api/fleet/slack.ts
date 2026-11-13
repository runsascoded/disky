/**
 * `POST /api/fleet/slack[?dry=1][&new=1]` (Bearer `FLEET_JOB_TOKEN`, the hourly cron's): read the fleet anew, build the
 * post (`_lib/slack.ts`) with a freshly signed card URL (a new `v` each run, so Slack fetches the image again), and
 * post it once, then edit it in place. `dry` answers the message without posting; `new` posts a new message in place of
 * the kept one. Answers `{ action, channel, ts, image }`.
 */
import { cardUrl } from '../../_lib/cardUrl.js'
import { type Ctx, json, same } from '../../_lib/env.js'
import { fleetDoc } from '../../_lib/fleet.js'
import { fleetMessage, publish } from '../../_lib/slack.js'

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const { env } = ctx
  const expected = env.FLEET_JOB_TOKEN
  if (!expected) return json({ error: 'FLEET_JOB_TOKEN is not set' }, 503)
  const presented = /^Bearer (.+)$/.exec(ctx.request.headers.get('authorization') ?? '')?.[1] ?? ''
  if (!same(presented, expected)) return json({ error: 'unauthenticated' }, 401)
  const unset = (['SESSION_SECRET', 'DISKY_SLACK_BOT_TOKEN', 'SLACK_FLEET_CHANNEL'] as const).filter(k => !env[k])
  const url = new URL(ctx.request.url)
  const dry = url.searchParams.has('dry')
  if (unset.length && !(dry && env.SESSION_SECRET)) return json({ error: `not configured: ${unset.join(', ')}` }, 503)
  const doc = await fleetDoc(env, { fresh: true, waitUntil: ctx.waitUntil })
  const image = await cardUrl(env.SESSION_SECRET!, url.origin, Math.floor(Date.now() / 1000), { v: String(doc.now) })
  const msg = fleetMessage(doc, image, `${url.origin}/fleet`)
  if (dry) return json({ action: 'dry', image, ...msg })
  try {
    return json({ ...await publish(env, msg, url.searchParams.has('new')), image })
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 502)
  }
}
