/**
 * disky's emoji vocabulary on the TS side (specs/emoji-vocabulary.md §1, §3): `emoji/vocab.json` is `dt-cloud emoji
 * vocab`'s output (`cloud/src/dt_cloud/emoji_vocab.py`, the source of truth; a test keeps them equal). A writer asks
 * for a key (`code`), never a literal; a custom emoji the posting workspace lacks is written as its key's stock one
 * (`fallback`), the workspace's custom names read once per run (`workspaceCustom`).
 */
import VOCAB from './emoji/vocab.json'

export interface VocabKey {
  /** The stock emoji, as characters */
  stock: string
  /** The stock emoji as Slack codes (`:name:`) */
  code: string
  /** The `disky` pack's custom image, as its Slack emoji name (no colons) */
  custom?: string
  /** Its Discord application-emoji name */
  discord?: string
  /** False: no writer produces it today */
  reachable?: boolean
}

export const KEYS: Record<string, VocabKey> = VOCAB

const BY_CUSTOM: Record<string, VocabKey> = Object.fromEntries(
  Object.values(KEYS).filter(k => k.custom).map(k => [k.custom!, k]),
)

/** The workspace's custom emoji names; null: unknown (every custom emoji written as it is). */
export type Have = ReadonlySet<string> | null

export function key(name: string): VocabKey {
  const k = KEYS[name]
  if (!k) throw new Error(`emoji vocabulary: no key ${JSON.stringify(name)}`)
  return k
}

/** Key `name` as Slack text: its custom `:name:` where it has one and the workspace has it (`have` null: assumed to),
 *  else its stock code. */
export function code(name: string, have: Have = null): string {
  const k = key(name)
  return k.custom && (have === null || have.has(k.custom)) ? `:${k.custom}:` : k.code
}

/** Key `name`'s stock emoji, as characters. */
export const stock = (name: string): string => key(name).stock

const CODE = /:([a-z0-9_+'-]{1,100}):/g

/** `text` with each vocabulary custom name the workspace lacks written as its key's stock code; unchanged when the
 *  workspace is unknown. Codes outside the vocabulary are left alone (they may be stock). */
export function fallback(text: string, have: Have): string {
  if (have === null) return text
  return text.replace(CODE, (m, name: string) => {
    const k = BY_CUSTOM[name]
    return k && !have.has(name) ? k.code : m
  })
}

/** Whether a custom `:name:` (a starter's own emoji, say) renders in the workspace: yes when it has it, or when the
 *  workspace is unknown. */
export const renders = (shortcode: string, have: Have): boolean => have === null || have.has(shortcode.replace(/^:|:$/g, ''))

/** Anything that calls a Slack Web API method (thrds' `SlackClient.api`). */
export interface SlackApi { api(method: string, body?: Record<string, unknown>): Promise<unknown> }

/** `emoji.list`'s `emoji` as name → image URL: an `alias:` takes its target's URL (followed through further aliases);
 *  an alias of a stock emoji, a dangling or circular one, and anything else not a URL are dropped. */
export function customEmojiUrls(raw: Record<string, unknown>): Record<string, string> {
  const resolve = (name: string, seen: ReadonlySet<string> = new Set()): string | null => {
    const v = Object.hasOwn(raw, name) ? raw[name] : undefined
    if (typeof v !== 'string' || seen.has(name)) return null
    if (v.startsWith('alias:')) return resolve(v.slice(6), new Set([...seen, name]))
    return v.startsWith('http') ? v : null
  }
  return Object.fromEntries(Object.keys(raw).flatMap(n => {
    const url = resolve(n)
    return url ? [[n, url]] : []
  }))
}

/** The workspace's custom emoji names (`emoji.list`: aliases followed, deleted ones dropped), read once; null, logged,
 *  when that fails (no `emoji:read`, network), so the post goes out as it always has. */
export async function workspaceCustom(slack: SlackApi, label: string): Promise<Have> {
  try {
    const r = await slack.api('emoji.list') as { emoji?: Record<string, unknown> }
    return new Set(Object.keys(customEmojiUrls(r.emoji ?? {})))
  } catch (e) {
    console.warn(`${label}: workspace emoji unread (${e instanceof Error ? e.message : String(e)}); custom emoji written as they are`)
    return null
  }
}
