/**
 * `POST /api/fleet/slack[?dry=1][&new=1]` (Bearer `FLEET_JOB_TOKEN`, the hourly cron's): read the fleet anew, build the
 * post (`_lib/slack.ts`), draw its card (`_lib/card.ts`, as `/og/fleet.png` draws the full one), and post it once with
 * the card attached, then edit it in place, swapping the card. `dry` answers the message, the card's size and the
 * replies it would keep, without posting; `new` posts a new message in place of the kept one. Answers
 * `{ action, channel, ts, file, undeleted }`, plus `split` (the reply carrying the card as an image per panel,
 * `publishSplit`) and `replies` (the image replies, `_lib/replies.ts`, each `{ action, ts }` or `{ error }`), or
 * `split_error` / `replies_error` when one failed as a whole. The post stands either way.
 */
import { fleetSvg, PANELS } from '../../_lib/card.js'
import { imageUrl } from '../../_lib/cardUrl.js'
import { type Ctx, json, same } from '../../_lib/env.js'
import { fleetDoc } from '../../_lib/fleet.js'
import { renderPng } from '../../_lib/png.js'
import { enabledReplies, publishReplies, replyImages, replyMessage } from '../../_lib/replies.js'
import { fleetMessage, parseEmoji, publish, publishSplit } from '../../_lib/slack.js'

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
  let emoji
  try {
    emoji = parseEmoji(env.FLEET_EMOJI)
  } catch (e) {
    return json({ error: errText(e) }, 503)
  }
  const doc = await fleetDoc(env, { fresh: true, waitUntil: ctx.waitUntil })
  const msg = fleetMessage(doc, `${url.origin}/fleet`, emoji)
  const deployments = doc.deployments.map(({ name, color }) => ({ name, color }))
  const input = { tier: 'full', theme: 'dark', deployments, doc, now: doc.now } as const
  const card = await renderPng(env, url.origin, fleetSvg(input))
  const panels = await Promise.all(PANELS.map(async (panel, only) => ({ panel, png: await renderPng(env, url.origin, fleetSvg({ ...input, only })) })))
  const replies = async () => {
    const out = []
    for (const r of replyImages(doc, enabledReplies(env.FLEET_REPLIES))) {
      const at = await imageUrl(env, url.origin, doc.now, { ...r.params, v: String(doc.now) })
      if (!at) throw new Error('the image replies need SESSION_SECRET (to sign their URLs) or FLEET_CARD_PUBLIC=1')
      out.push({ key: r.key, msg: replyMessage(r, at) })
    }
    return out
  }
  if (dry) {
    const planned = await replies().then(rs => rs.map(r => r.key), e => ({ error: errText(e) }))
    return json({ action: 'dry', card_bytes: card.byteLength, panel_bytes: panels.map(p => p.png.byteLength), replies: planned, ...msg })
  }
  let posted
  try {
    posted = await publish(env, msg, card, url.searchParams.has('new'))
  } catch (e) {
    return json({ error: errText(e) }, 502)
  }
  const out: Record<string, unknown> = { ...posted }
  try {
    out.split = await publishSplit(env, posted, panels)
  } catch (e) {
    out.split_error = errText(e)
  }
  try {
    out.replies = await publishReplies(env, posted, await replies())
  } catch (e) {
    out.replies_error = errText(e)
  }
  return json(out)
}

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))
