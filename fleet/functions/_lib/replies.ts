/**
 * The fleet post's image replies (specs/disky-oa-dev.md, "Replies", #78): more views of the fleet, each its own reply
 * under the post, kept for a while to see which proves useful. Each is a Block Kit `image` block on a signed
 * `/og/fleet.png?view=…` URL (not an attached file: #80), posted once per post, then edited in place each hourly run
 * with a new `v`, so Slack fetches the image anew.
 *
 * - `month`: the card's 4 panels over the page's whole window (`HISTORY_DAYS`);
 * - `timeline`: each deployment's runs over the week as start→end bars by outcome (`views.ts` `timelineSvg`);
 * - `cost`: each deployment's scan cost this month, cumulative, against last month's (`costSvg`);
 * - `deployments`: one reply per deployment, its newest runs listed as its quiet run thread lists them (`slack.ts`
 *   `trailLines`) over its status card (`deployCard.ts`: its last runs stacked by step, and a strip of 4 sparklines).
 *
 * `FLEET_REPLIES` (comma-separated kinds; unset: all, empty: none) drops a kind without a code change. A dropped kind's
 * reply stays in the thread, as it last was; delete it by hand.
 */
import type { Have } from '@disk-tree/pages-kit/emoji'
import { SlackApiError, SlackClient } from '@open-athena/thrds'
import { fleetSvg, type Theme, ZOOM } from './card.js'
import { CARD_RUNS, DEPLOY_ZOOM, deployCardInput, deployCardSvg, STRIP_DAYS } from './deployCard.js'
import type { Env } from './env.js'
import { HISTORY_DAYS } from './fleet.js'
import type { FleetDoc } from './fleetTypes.js'
import { type Block, DEFAULT_EMOJI, type Emoji, SECTION_MAX, type SlackMessage, trailLines } from './slack.js'
import { costSvg, timelineSvg } from './views.js'

export const REPLY_KINDS = ['month', 'timeline', 'cost', 'deployments'] as const
export type ReplyKind = (typeof REPLY_KINDS)[number]

/** `FLEET_REPLIES` as the kinds it enables: unset is every kind, empty none; an unknown kind raises (a typo would
 *  otherwise drop a reply silently). */
export function enabledReplies(raw: string | undefined): ReplyKind[] {
  if (raw === undefined) return [...REPLY_KINDS]
  const kinds = raw.split(',').map(k => k.trim()).filter(Boolean)
  const bad = kinds.filter(k => !(REPLY_KINDS as readonly string[]).includes(k))
  if (bad.length) throw new Error(`FLEET_REPLIES: unknown ${bad.join(', ')} (kinds: ${REPLY_KINDS.join(', ')})`)
  return REPLY_KINDS.filter(k => kinds.includes(k))
}

/** One reply: its key (where its ts is kept), its text (and lines under it), its image's alt text and the image's view
 *  params. */
export interface ReplyImage {
  key: string
  text: string
  lines?: string[]
  alt: string
  params: Record<string, string>
}

/** The enabled replies, in posting order (a deployment's card in the post's order); `emoji` and `have` as the post's
 *  (`slack.ts` `fleetMessage`), for the deployments' run lines. */
export function replyImages(doc: FleetDoc, kinds: ReplyKind[], emoji: Emoji = DEFAULT_EMOJI, have: Have = null): ReplyImage[] {
  const out: ReplyImage[] = []
  for (const kind of kinds) {
    if (kind === 'month') {
      out.push({ key: 'month', params: { view: 'month' }, alt: `disky fleet: /meta store size, objects, scan duration and cost per day, per deployment, over the last ${HISTORY_DAYS} days`,
        text: `*The card over ${HISTORY_DAYS} days*: the post's four panels across the fleet page's whole window. Edited in place hourly.` })
    } else if (kind === 'timeline') {
      out.push({ key: 'timeline', params: { view: 'timeline' }, alt: 'disky fleet: each deployment\'s scan runs over the last week, start to end, coloured by outcome',
        text: '*Scan timeline, last 7 days*: each run from start to end, coloured by outcome, so overlaps, long runs and gaps show. Edited in place hourly.' })
    } else if (kind === 'cost') {
      out.push({ key: 'cost', params: { view: 'cost' }, alt: 'disky fleet: each deployment\'s scan cost this month, cumulative, against last month',
        text: '*Scan cost by month*: each deployment\'s running total this month (solid) against last month (dashed). Edited in place hourly.' })
    } else {
      for (const d of doc.deployments) {
        const lines = trailLines(d, emoji, have)
        const what = lines.length ? `its last ${lines.length === 1 ? 'run' : `${lines.length} runs`}, oldest first` : d.errors.runs ? `runs n/a (${d.errors.runs})` : 'no runs'
        out.push({
          key: `dep:${d.name}`, params: { view: 'deploy', d: d.name }, lines,
          alt: `${d.name}: its last ${CARD_RUNS} scan runs' durations, stacked by step, and its data, objects, cost per run and run time over ${STRIP_DAYS} days`,
          text: `*<${d.url}|${d.name}>*: ${what}. <${d.url}/scans|All scans>. Edited in place hourly.`,
        })
      }
    }
  }
  return out
}

/** The SVG `/og/fleet.png` draws for a full-tier `view` (`d`: a deployment's name), or null for no such view. Without a
 *  view, the card; `deploy`, a deployment's status card (`deployCard.ts`). */
export function viewSvg(view: string | null, dep: string | null, c: { theme: Theme; doc: FleetDoc; deployments: { name: string; color: string }[] }): string | null {
  const base = { tier: 'full', theme: c.theme, deployments: c.deployments, doc: c.doc, now: c.doc.now } as const
  switch (view ?? 'card') {
    case 'card': return fleetSvg(base)
    case 'month': return fleetSvg({ ...base, days: HISTORY_DAYS, title: `disky fleet · ${HISTORY_DAYS} days` })
    case 'timeline': return timelineSvg(c.doc, c.theme)
    case 'cost': return costSvg(c.doc, c.theme)
    case 'deploy': {
      const d = c.doc.deployments.find(x => x.name === dep)
      return d ? deployCardSvg(deployCardInput(d, c.doc.now, c.theme)) : null
    }
    default: return null
  }
}

/** The scale a view's PNG is rasterized at: the deployment card is drawn narrower (`DEPLOY_W`), so at more. */
export const viewZoom = (view: string | null): number => (view === 'deploy' ? DEPLOY_ZOOM : ZOOM)

/** Lines joined into as few texts as fit a section (`SECTION_MAX`), in order. */
export function packLines(lines: string[], max = SECTION_MAX): string[] {
  const out: string[] = []
  for (const l of lines) {
    if (out.length && out[out.length - 1].length + 1 + l.length <= max) out[out.length - 1] += `\n${l}`
    else out.push(l)
  }
  return out
}

const section = (text: string): Block => ({ type: 'section', text: { type: 'mrkdwn', text } })

/** A reply's message: its text, its lines (a section each `SECTION_MAX`), then its image. */
export const replyMessage = (r: ReplyImage, url: string): SlackMessage => ({
  text: r.text.replace(/\*/g, ''),
  blocks: [section(r.text), ...packLines(r.lines ?? []).map(section), { type: 'image', image_url: url, alt_text: r.alt }],
})

/** Where the replies' ts are kept between runs. */
export const REPLIES_KV_KEY = 'slack-fleet-replies'

/** What's kept: the post they reply to, and each reply's ts by key. */
interface Kept { channel: string; thread: string; ts: Record<string, string> }

export type ReplyResult = { action: 'posted' | 'updated'; ts: string } | { error: string }

const gone = (e: unknown) => e instanceof SlackApiError && e.code === 'message_not_found'
const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** Keep each reply under the post (`op`): posted once (its ts kept by key), then edited in place each run; a kept reply
 *  deleted by hand is posted again. Replies kept for an earlier post are left where they are. One reply's failure is
 *  its own result, never the others' or the post's. */
export async function publishReplies(
  env: Env,
  op: { channel: string; ts: string },
  replies: { key: string; msg: SlackMessage }[],
  client: (channel: string) => SlackClient = channel => new SlackClient({ token: env.DISKY_SLACK_BOT_TOKEN!, channel }),
): Promise<Record<string, ReplyResult>> {
  if (!env.FLEET_KV) throw new Error('the replies need FLEET_KV, to keep their ts')
  const slack = client(op.channel)
  const was = await env.FLEET_KV.get<Kept>(REPLIES_KV_KEY, 'json')
  const kept: Kept = was && was.channel === op.channel && was.thread === op.ts ? was : { channel: op.channel, thread: op.ts, ts: {} }
  const out: Record<string, ReplyResult> = {}
  for (const { key, msg } of replies) {
    try {
      const ts = kept.ts[key]
      if (ts) {
        try {
          await slack.edit(ts, msg)
          out[key] = { action: 'updated', ts }
          continue
        } catch (e) {
          if (!gone(e)) throw e
        }
      }
      const posted = await slack.post(msg, op.ts)
      kept.ts[key] = posted.id
      await env.FLEET_KV.put(REPLIES_KV_KEY, JSON.stringify(kept))
      out[key] = { action: 'posted', ts: posted.id }
    } catch (e) {
      out[key] = { error: errText(e) }
    }
  }
  return out
}
