/**
 * The fleet's Slack post (specs/disky-oa-dev.md): a line per deployment (its newest scan, how it went, how long, what
 * it cost, its `/meta` size and the week's change), the card as an `image` block, and when it was read. One message,
 * posted once, then edited in place each run (`chat.update`): its channel + ts are kept in `FLEET_KV`, or pinned by
 * `SLACK_FLEET_TS`.
 */
import { fmtSI } from '@disk-tree/pages-kit/og/text'
import { fmtHours, fmtUsd } from './card.js'
import type { Env } from './env.js'
import type { FleetDeployment, FleetDoc } from './fleet.js'

export type Block = Record<string, unknown>
export interface SlackMessage { text: string; blocks: Block[] }

/** A deployment's state, in a few words, and its emoji. */
export function status(d: FleetDeployment): { emoji: string; word: string } {
  const r = d.last
  if (!r) {
    if (d.errors.runs) return { emoji: ':grey_question:', word: `unreachable (${d.errors.runs})` }
    return { emoji: ':white_circle:', word: 'no scans' }
  }
  if (r.status === 'running') return { emoji: ':hourglass_flowing_sand:', word: 'running' }
  if (r.status === 'failed') return { emoji: ':x:', word: r.failed_at ? `failed at ${r.failed_at}` : 'failed' }
  const n = r.open_issues.length
  if (n) return { emoji: ':warning:', word: `${n} step issue${n > 1 ? 's' : ''} open (${r.open_issues.join(', ')})` }
  return { emoji: ':white_check_mark:', word: 'ok' }
}

/** A signed byte change: `+1.2 GB`, `−300 MB`. */
const delta = (b: number): string => `${b < 0 ? '−' : '+'}${fmtSI(Math.abs(b))}`

/** Seconds as a short age: `40m`, `9h`, `3d`. */
export const fmtAge = (s: number): string => (s < 3600 ? `${Math.round(s / 60)}m` : s < 2 * 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`)

/** One deployment's line. */
export function line(d: FleetDeployment): string {
  const { emoji, word } = status(d)
  const parts: string[] = []
  const r = d.last
  if (r) parts.push(`\`${r.scan}\``, word)
  else parts.push(word)
  if (r && r.status !== 'running' && r.secs !== null) parts.push(fmtHours(r.secs))
  if (r) parts.push(r.cost_usd === null ? 'cost n/a' : fmtUsd(r.cost_usd))
  const m = d.meta
  if (m && m.now !== null) parts.push(`/meta ${fmtSI(m.now)}${m.d7 !== null ? ` (${delta(m.now - m.d7)} 7d)` : ''}`)
  else parts.push('/meta n/a')
  if (d.health?.newest_scan_age != null && d.health.newest_scan_age > 36 * 3600) parts.push(`newest scan ${fmtAge(d.health.newest_scan_age)} old`)
  return `${emoji} *<${d.url}|${d.name}>*  ${parts.join(' · ')}`
}

const stamp = (t: number) => `${new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`

/** The post: its fallback text and blocks. `image` is the signed card URL, `page` the fleet page. */
export function fleetMessage(doc: FleetDoc, image: string, page: string): SlackMessage {
  const lines = doc.deployments.map(line)
  return {
    text: `disky fleet: ${doc.deployments.map(d => `${d.name} ${status(d).word}`).join(', ')}`,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: [`*disky fleet* · <${page}|details>`, ...lines].join('\n') } },
      { type: 'image', image_url: image, alt_text: 'disky fleet: /meta store size, scan duration and cost per scan, per deployment, over the last week' },
      { type: 'context', elements: [{ type: 'mrkdwn', text: `read ${stamp(doc.now)} · updated in place hourly` }] },
    ],
  }
}

interface SlackResult { ok: boolean; error?: string; ts?: string; channel?: string }

async function slackApi(token: string, method: string, body: Record<string, unknown>, f: typeof fetch): Promise<SlackResult> {
  const r = await f(`https://slack.com/api/${method}`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json; charset=utf-8' },
    body: JSON.stringify(body),
  })
  if (!r.ok) return { ok: false, error: `HTTP ${r.status}` }
  return r.json() as Promise<SlackResult>
}

/** Where the post is kept between runs. */
export const KV_KEY = 'slack-fleet-post'

export interface Posted { action: 'posted' | 'updated'; channel: string; ts: string }

export const NO_POST = 'no post to update: bind FLEET_KV, or post one with `?new=1` and set SLACK_FLEET_TS to its ts'

/** Post `msg`, or edit the post already there (`FLEET_KV`'s, else `SLACK_FLEET_TS` in `SLACK_FLEET_CHANNEL`). A new post
 *  (none yet, or the kept one deleted by hand) needs somewhere to keep its ts (`FLEET_KV`) or `fresh`, the explicit ask
 *  for one: without either, an hourly run would post a new message every hour. */
export async function publish(env: Env, msg: SlackMessage, fresh = false, f: typeof fetch = fetch): Promise<Posted> {
  const token = env.DISKY_SLACK_BOT_TOKEN!
  const kept = env.FLEET_KV ? await env.FLEET_KV.get<{ channel: string; ts: string }>(KV_KEY, 'json') : null
  const at = fresh ? null : kept ?? (env.SLACK_FLEET_TS ? { channel: env.SLACK_FLEET_CHANNEL!, ts: env.SLACK_FLEET_TS } : null)
  if (at) {
    const r = await slackApi(token, 'chat.update', { channel: at.channel, ts: at.ts, ...msg }, f)
    if (r.ok) return { action: 'updated', channel: at.channel, ts: at.ts }
    if (r.error !== 'message_not_found') throw new Error(`chat.update: ${r.error}`)
  }
  if (!env.FLEET_KV && !fresh) throw new Error(NO_POST)
  const r = await slackApi(token, 'chat.postMessage', { channel: env.SLACK_FLEET_CHANNEL, ...msg }, f)
  if (!r.ok || !r.ts || !r.channel) throw new Error(`chat.postMessage: ${r.error ?? 'no ts'}`)
  if (env.FLEET_KV) await env.FLEET_KV.put(KV_KEY, JSON.stringify({ channel: r.channel, ts: r.ts }))
  return { action: 'posted', channel: r.channel, ts: r.ts }
}
