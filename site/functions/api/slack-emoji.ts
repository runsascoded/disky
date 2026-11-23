/** The Slack workspace's custom emoji, for `/admin/slack`'s digest re-apply review (thrds' `PlanView` draws a message's
 *  `:arrow_deg80:` / `:runway-3:` as its image; specs/done/digest-rewrite-review.md).
 *
 *   GET /api/slack-emoji → {emoji: {name: url}}                                                              admin
 *
 * `emoji.list` with the site's `SLACK_BOT_TOKEN`, an `alias:` resolved to its target's URL (`customEmojiUrls`), read at
 * most once per `EMOJI_TTL_MS` per isolate and token (the list is large). No token, or one without `emoji:read` (or any
 * failed read): `{emoji: {}}`, logged once (per isolate; per read for a failure, which is cached as the list is), and the
 * page shows each custom emoji as its `:name:`. */
import { customEmojiUrls } from '@disk-tree/pages-kit/emoji'
import { SlackClient } from '@open-athena/thrds'
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'

export const EMOJI_TTL_MS = 5 * 60_000

type Emoji = Record<string, string>

let cached: { token: string; at: number; emoji: Promise<Emoji> } | null = null
let warnedNoToken = false

/** Forget the cached list and the logged warning (tests). */
export const resetSlackEmoji = (): void => { cached = null; warnedNoToken = false }

async function read(token: string): Promise<Emoji> {
  try {
    const r = await new SlackClient({ token }).api('emoji.list') as { emoji?: Record<string, unknown> }
    return customEmojiUrls(r.emoji ?? {})
  } catch (e) {
    console.warn(`slack-emoji: emoji.list failed (${e instanceof Error ? e.message : String(e)}); custom emoji show as their names`)
    return {}
  }
}

/** The workspace's custom emoji, name → image URL, through the per-isolate cache. */
export function workspaceEmoji(token: string | undefined, now: number = Date.now()): Promise<Emoji> {
  if (!token) {
    if (!warnedNoToken) console.warn('slack-emoji: no SLACK_BOT_TOKEN; custom emoji show as their names')
    warnedNoToken = true
    return Promise.resolve({})
  }
  if (!cached || cached.token !== token || now - cached.at >= EMOJI_TTL_MS) cached = { token, at: now, emoji: read(token) }
  return cached.emoji
}

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as unknown as { SLACK_BOT_TOKEN?: string }
  return json({ emoji: await workspaceEmoji(env.SLACK_BOT_TOKEN) }, 200, { 'cache-control': 'no-store' })
}
