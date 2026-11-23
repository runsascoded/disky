/**
 * The fleet's Slack post (specs/disky-oa-dev.md): a line per deployment (its newest scan, linked to its run, how it went, how long, what
 * it cost, its `/meta` size and the week's change), when it was read, and the card as an attached PNG. One message,
 * posted once, then edited in place each run: its channel, ts and card file are kept in `FLEET_KV`, or the post pinned
 * by `SLACK_FLEET_TS`. The card is an attached file, not an `image` block, because Slack's phone apps offer Download
 * only on a file (Ryan, 2026-11-20); each run uploads the new card, attaches it in the same edit, then deletes the
 * previous one (`@open-athena/thrds` `replaceFile`: no orphaned uploads).
 */
import { code, type Have, renders } from '@disk-tree/pages-kit/emoji'
import { fmtSI } from '@disk-tree/pages-kit/og/text'
import { FilesNotAttachedError, FileSwapError, SlackApiError, SlackClient } from '@open-athena/thrds'
import { fmtHours, fmtUsd } from './card.js'
import type { Env } from './env.js'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleet.js'
import { type Outcome, runOutcome } from './runOutcome.js'

export type Block = Record<string, unknown>
export interface SlackMessage { text: string; blocks: Block[] }

/** Each run outcome's emoji in the post: the vocabulary's `run.*` (`@disk-tree/pages-kit/emoji`); `FLEET_EMOJI`
 *  (`parseEmoji`) overrides any of them. */
export type Emoji = Record<Outcome, string>
export const DEFAULT_EMOJI: Emoji = {
  running: code('run.running'), cancelled: code('run.cancelled'), failed: code('run.failed'), issues: code('run.issues'), ok: code('run.clean'),
}

/** `FLEET_EMOJI`'s keys, each the outcome it names (`warn`: a step issue still open). */
export const EMOJI_KEYS: Record<string, Outcome> = { ok: 'ok', warn: 'issues', failed: 'failed', running: 'running', cancelled: 'cancelled' }

/** A Slack emoji shortcode, `:name:`. */
const SHORTCODE = /^:[a-z0-9_+'-]{1,100}:$/

/** `FLEET_EMOJI` (a JSON object: `EMOJI_KEYS` → `:shortcode:`) over `DEFAULT_EMOJI`; unset is the defaults. Anything else
 *  raises: a typo'd key or a bare word would silently print in every line. */
export function parseEmoji(raw: string | undefined): Emoji {
  if (raw === undefined || raw.trim() === '') return DEFAULT_EMOJI
  const o: unknown = JSON.parse(raw)
  if (!o || typeof o !== 'object' || Array.isArray(o)) throw new Error('FLEET_EMOJI: not a JSON object')
  const out = { ...DEFAULT_EMOJI }
  for (const [k, v] of Object.entries(o)) {
    const outcome = EMOJI_KEYS[k]
    if (!outcome) throw new Error(`FLEET_EMOJI: unknown key ${JSON.stringify(k)} (keys: ${Object.keys(EMOJI_KEYS).join(', ')})`)
    if (typeof v !== 'string' || !SHORTCODE.test(v)) throw new Error(`FLEET_EMOJI.${k}: not a :shortcode: (${JSON.stringify(v)})`)
    out[outcome] = v
  }
  return out
}

/** A run's outcome (`runOutcome`), in a few words, and its emoji. */
function runStatus(r: FleetRun, emoji: Emoji = DEFAULT_EMOJI): { emoji: string; word: string } {
  const n = r.open_issues.length
  const o = runOutcome(r)
  switch (o) {
    case 'running': return { emoji: emoji[o], word: 'running' }
    case 'cancelled': return { emoji: emoji[o], word: 'cancelled' }
    case 'failed': return { emoji: emoji[o], word: r.failed_at ? `failed at ${r.failed_at}` : 'failed' }
    case 'issues': return { emoji: emoji[o], word: `${n} step issue${n > 1 ? 's' : ''} open (${r.open_issues.join(', ')})` }
    case 'ok': return { emoji: emoji[o], word: 'ok' }
  }
}

/** A deployment's state (its newest run's, else why it has none), in a few words, and its emoji. */
export function status(d: FleetDeployment, emoji: Emoji = DEFAULT_EMOJI): { emoji: string; word: string } {
  if (d.last) return runStatus(d.last, emoji)
  if (d.errors.runs) return { emoji: ':grey_question:', word: `unreachable (${d.errors.runs})` }
  return { emoji: ':white_circle:', word: 'no scans' }
}

/** How many of a deployment's newest runs its line shows as a strip. */
export const STRIP_RUNS = 10

/** The newest `STRIP_RUNS` runs' emoji, newest first (the newest is the deployment's state, so the line shows it once);
 *  its newest run alone when it lists none, else its state's emoji. Consecutive runs started the same way form a group,
 *  their statuses run together, and groups are set off by spaces. A scheduled group (or one with no recorded trigger) is
 *  its statuses alone; a manual group ends in who started it (`starter`), with no space or parentheses, so the face
 *  reads as its group's: `⏳✅✅ ✅⚠️:ryan: ✅` (Ryan, 2026-11-23: a face after every manual run repeated it, and the
 *  space inside `✅ (:ryan:)` matched the space between runs). Not linked: Slack's phone apps print a link whose label is
 *  an emoji as its bare URL, then the emoji (Ryan, 2026-11-20). Each run is a link on `/fleet`. */
const strip = (d: FleetDeployment, emoji: Emoji, have: Have): string => {
  const runs = d.runs.length ? d.runs.slice(0, STRIP_RUNS) : d.last ? [d.last] : []
  if (!runs.length) return status(d, emoji).emoji
  const groups: { who: string; statuses: string }[] = []
  for (const r of runs) {
    const who = r.trigger === 'manual' ? starter(r, have) : ''
    const s = runStatus(r, emoji).emoji
    const last = groups[groups.length - 1]
    if (last && last.who === who) last.statuses += s
    else groups.push({ who, statuses: s })
  }
  return groups.map(g => g.statuses + g.who).join(' ')
}

/** A signed byte change: `+1.2 GB`, `−300 MB`. */
const delta = (b: number): string => `${b < 0 ? '−' : '+'}${fmtSI(Math.abs(b))}`

/** Seconds as a short age: `40m`, `9h`, `3d`. */
export const fmtAge = (s: number): string => (s < 3600 ? `${Math.round(s / 60)}m` : s < 2 * 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`)

/** Who started a manual run, as the scan-health thread shows it: their own emoji (`trigger_emoji`) where the workspace
 *  has it (`have`), else 👤. */
const starter = (r: FleetRun, have: Have): string =>
  (r.trigger_emoji && renders(r.trigger_emoji, have) && r.trigger_emoji) || code('trigger.manual')

/** How many runs a deployment's reply under the post lists: as many as its quiet run thread's rolling reply
 *  (`dt_cloud.nominal` `trail`). */
export const TRAIL_RUNS = 10

const two = (n: number) => String(n).padStart(2, '0')

/** A run's start as the run threads stamp it: `11/22 07:00Z`. */
export function runStamp(t: number): string {
  const d = new Date(t * 1000)
  return `${two(d.getUTCMonth() + 1)}/${two(d.getUTCDate())} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}Z`
}

/** A run's duration as the run threads give it: `115m 54s`, or `duration unknown`. */
export const took = (secs: number | null): string =>
  (secs === null ? 'duration unknown' : `${Math.floor(secs / 60)}m ${two(Math.floor(secs) % 60)}s`)

/** A finished run that isn't ok, in a few words and why (`nominal`'s `describe`): `failed in <step>: <error>`,
 *  `cancelled`, `phase issues: <steps>`; null for an ok run. */
function why(r: FleetRun): string | null {
  switch (runOutcome(r)) {
    case 'failed': {
      const where = !r.failed_at ? '' : r.failed_at.startsWith('after ') ? ` ${r.failed_at}` : ` in ${r.failed_at}`
      return `failed${where}${r.error ? `: ${r.error.replace(/&/g, '&amp;')}` : ''}`
    }
    case 'cancelled': return 'cancelled'
    case 'issues': return `phase issues: ${[...new Set(r.open_issues)].sort().join(', ')}`
    default: return null
  }
}

/** A run's line in its deployment's reply, as its quiet run thread's rolling reply lists it (`dt_cloud.nominal`
 *  `reply` / `running`): its outcome's emoji, its trigger (a manual run's starter, `starter`), its start linked to its
 *  record, then `running`, or its duration and, when it isn't ok, why. */
export function runLine(d: Pick<FleetDeployment, 'url'>, r: FleetRun, emoji: Emoji = DEFAULT_EMOJI, have: Have = null): string {
  const o = runOutcome(r)
  const trigger = r.trigger === 'manual' ? starter(r, have) : code(r.trigger === 'schedule' ? 'trigger.schedule' : 'trigger.unknown')
  const head = `${emoji[o]} ${trigger} <${d.url}/scans/${encodeURIComponent(r.run_id)}|${r.started === null ? r.scan : runStamp(r.started)}>`
  if (o === 'running') return `${head} · running`
  const w = why(r)
  return `${head} · ${took(r.secs)}${w ? ` · ${w}` : ''}`
}

/** A deployment's newest `TRAIL_RUNS` runs' lines (`runLine`), oldest first, so the runs going now come last. */
export const trailLines = (d: FleetDeployment, emoji: Emoji = DEFAULT_EMOJI, have: Have = null): string[] =>
  d.runs.slice(0, TRAIL_RUNS).reverse().map(r => runLine(d, r, emoji, have))

/** One deployment's line: its name (a link), then its run strip (`strip`, newest first), then its newest run and the
 *  rest. `have`: the workspace's custom emoji (null: unknown). */
export function line(d: FleetDeployment, emoji: Emoji = DEFAULT_EMOJI, have: Have = null): string {
  const { word } = status(d, emoji)
  const parts: string[] = []
  const r = d.last
  if (r) parts.push(`<${d.url}/scans/${encodeURIComponent(r.run_id)}|${r.scan}>`, word)
  else parts.push(word)
  if (r && r.status !== 'running' && r.secs !== null) parts.push(fmtHours(r.secs))
  if (r && r.status !== 'running') parts.push(r.cost_usd === null ? 'cost n/a' : fmtUsd(r.cost_usd))
  const m = d.meta
  const meta = `<${d.url}/meta|/meta>`
  if (m && m.now !== null) parts.push(`${meta} ${fmtSI(m.now)}${m.d7 !== null ? ` (${delta(m.now - m.d7)} 7d)` : ''}`)
  else parts.push(`${meta} n/a`)
  if (d.health?.newest_scan_age != null && d.health.newest_scan_age > 36 * 3600) parts.push(`newest scan ${fmtAge(d.health.newest_scan_age)} old`)
  // Where to read more: its `/scans`, and the run thread in `#<deployment>-admin` (its newest run reported there)
  parts.push(`<${d.url}/scans|scans>`)
  const thread = [d.last, ...d.runs].find(x => x?.thread_url)?.thread_url
  if (thread) parts.push(`<${thread}|thread>`)
  if (d.channel) parts.push(`<#${d.channel}>`)
  return `*<${d.url}|${d.name}>*  ${strip(d, emoji, have)}  ${parts.join(' · ')}`
}

const stamp = (t: number) => `${new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`

/** Slack refuses a section whose text is longer than this (`invalid_blocks`): every edit failed while the four lines
 *  shared one section once each carried its 10 run links (2026-11-20, 00:30–02:00 UTC). */
export const SECTION_MAX = 3000

const section = (text: string) => ({ type: 'section', text: { type: 'mrkdwn', text } })

/** The post: its fallback text and blocks, a section per deployment (each well under `SECTION_MAX`); `page` is the fleet
 *  page, `have` the workspace's custom emoji (`workspaceCustom`; null: unknown). The card goes with it as an attached
 *  file (`publish`). */
export function fleetMessage(doc: FleetDoc, page: string, emoji: Emoji = DEFAULT_EMOJI, have: Have = null): SlackMessage {
  return {
    text: `disky fleet: ${doc.deployments.map(d => `${d.name} ${status(d).word}`).join(', ')}`,
    blocks: [
      section(`*disky fleet* · <${page}|details>`),
      ...doc.deployments.map(d => section(line(d, emoji, have))),
      { type: 'context', elements: [{ type: 'mrkdwn', text: `read ${stamp(doc.now)} · updated in place hourly` }] },
    ],
  }
}

/** Where the post is kept between runs. */
export const KV_KEY = 'slack-fleet-post'

/** The post's card: its file name (what Download saves) and alt text. */
export const CARD_NAME = 'disky-fleet.png'
export const CARD_ALT = 'disky fleet: /meta store size, objects, scan duration and cost per day, per deployment, over the last week'

/** What's kept between runs: the post, and the card file attached to it (so the next run deletes it after its swap). */
interface Kept { channel: string; ts: string; file?: string }

export interface Posted {
  action: 'posted' | 'updated'
  channel: string
  ts: string
  /** The card file now attached. */
  file: string
  /** Earlier cards whose delete failed (other than already gone): delete them by hand. */
  undeleted: string[]
}

export const NO_POST = 'no post to update: bind FLEET_KV, or post one with `?new=1` and set SLACK_FLEET_TS to its ts'

const missing = (e: unknown) => e instanceof FileSwapError && e.cause instanceof SlackApiError && e.cause.code === 'message_not_found'

/** Post `msg` with `card` attached, or edit the post already there (`FLEET_KV`'s, else `SLACK_FLEET_TS` in
 *  `SLACK_FLEET_CHANNEL`), swapping its card. A new post (none yet, or the kept one deleted by hand) needs somewhere to
 *  keep its ts (`FLEET_KV`) or `fresh`, the explicit ask for one: without either, an hourly run would post a new message
 *  every hour. `client` makes the channel's Slack client (tests pass a fake). */
export async function publish(
  env: Env,
  msg: SlackMessage,
  card: Uint8Array,
  fresh = false,
  client: (channel: string) => SlackClient = channel => new SlackClient({ token: env.DISKY_SLACK_BOT_TOKEN!, channel }),
): Promise<Posted> {
  const keep = async (k: Kept) => { if (env.FLEET_KV) await env.FLEET_KV.put(KV_KEY, JSON.stringify(k)) }
  const kept = env.FLEET_KV ? await env.FLEET_KV.get<Kept>(KV_KEY, 'json') : null
  const at: Kept | null = fresh ? null : kept ?? (env.SLACK_FLEET_TS ? { channel: env.SLACK_FLEET_CHANNEL!, ts: env.SLACK_FLEET_TS } : null)
  const file = { name: CARD_NAME, data: card, altText: CARD_ALT }
  if (at) {
    try {
      // the kept card is the one to delete; without a record of it, the swap reads the message's own files
      const swap = await client(at.channel).replaceFile(at.ts, file, msg, at.file ? { previous: [{ id: at.file }] } : {})
      await keep({ channel: at.channel, ts: at.ts, file: swap.fileId })
      return { action: 'updated', channel: at.channel, ts: at.ts, file: swap.fileId, undeleted: swap.undeleted.map(u => u.id) }
    } catch (e) {
      if (!missing(e)) throw e
    }
  }
  if (!env.FLEET_KV && !fresh) throw new Error(NO_POST)
  const channel = env.SLACK_FLEET_CHANNEL!
  const slack = client(channel)
  const id = await slack.upload(file.name, file.data, { altText: file.altText })
  try {
    const posted = await slack.post({ ...msg, files: [id] })
    await keep({ channel, ts: posted.id, file: id })
    return { action: 'posted', channel, ts: posted.id, file: id, undeleted: [] }
  } catch (e) {
    // posted, but the card didn't attach: keep the post (no second one next hour) and let the next run's swap attach a card
    if (e instanceof FilesNotAttachedError) await keep({ channel, ts: e.ts })
    else await slack.deleteFile(id)
    throw e
  }
}

/** Where the split card's reply is kept between runs. */
export const SPLIT_KV_KEY = 'slack-fleet-split'

/** The split card: each panel's file name and alt text (`card.ts` `PANELS`). */
export const splitName = (panel: string) => `disky-fleet-${panel}.png`
const SPLIT_ALT: Record<string, string> = {
  meta: 'disky fleet: /meta store size per deployment, over the last week',
  objects: 'disky fleet: objects in each deployment\'s primary store, over the last week',
  duration: 'disky fleet: scan duration per run, per deployment, over the last week',
  cost: 'disky fleet: scan cost per complete UTC day, per deployment, over the last week',
}

/** The split card's reply text: what it is, beside the post's single card. */
export const SPLIT_TEXT = 'The card split into an image per panel (swipe), to compare with the single card above; edited in place hourly.'

/** What's kept of the split card's reply: the post it replies to, its ts and its files, in panel order. */
interface SplitKept { channel: string; thread: string; ts: string; files: string[] }

export interface SplitPosted { action: 'posted' | 'updated'; ts: string; files: string[]; undeleted: string[] }

/** Keep a reply under the post (`op`) carrying the card as one attached image per panel (`panels`, in `PANELS` order):
 *  posted once per post, then its files swapped in place each run, as `publish` swaps the post's single card. A
 *  comparison beside the single card (Ryan, 2026-11-20); a reply kept for an earlier post is left where it is. */
export async function publishSplit(
  env: Env,
  op: { channel: string; ts: string },
  panels: { panel: string; png: Uint8Array }[],
  client: (channel: string) => SlackClient = channel => new SlackClient({ token: env.DISKY_SLACK_BOT_TOKEN!, channel }),
): Promise<SplitPosted> {
  if (!env.FLEET_KV) throw new Error('the split card needs FLEET_KV, to keep its reply')
  const slack = client(op.channel)
  const files = panels.map(({ panel, png }) => ({ name: splitName(panel), data: png, altText: SPLIT_ALT[panel] ?? panel }))
  const msg = { text: SPLIT_TEXT }
  const kept = await env.FLEET_KV.get<SplitKept>(SPLIT_KV_KEY, 'json')
  if (kept && kept.channel === op.channel && kept.thread === op.ts) {
    try {
      const swap = await slack.replaceFiles(kept.ts, files, msg, { previous: kept.files.map(id => ({ id })) })
      await env.FLEET_KV.put(SPLIT_KV_KEY, JSON.stringify({ ...kept, files: swap.fileIds }))
      return { action: 'updated', ts: kept.ts, files: swap.fileIds, undeleted: swap.undeleted.map(u => u.id) }
    } catch (e) {
      if (!missing(e)) throw e
    }
  }
  const ids: string[] = []
  try {
    for (const f of files) ids.push(await slack.upload(f.name, f.data, { altText: f.altText }))
    const posted = await slack.post({ ...msg, files: ids }, op.ts)
    await env.FLEET_KV.put(SPLIT_KV_KEY, JSON.stringify({ channel: op.channel, thread: op.ts, ts: posted.id, files: ids }))
    return { action: 'posted', ts: posted.id, files: ids, undeleted: [] }
  } catch (e) {
    // posted without its files: keep the reply (no second one next hour), and let the next run's swap attach them
    if (e instanceof FilesNotAttachedError) await env.FLEET_KV.put(SPLIT_KV_KEY, JSON.stringify({ channel: op.channel, thread: op.ts, ts: e.ts, files: [] }))
    else for (const id of ids) await slack.deleteFile(id).catch(() => {})
    throw e
  }
}
