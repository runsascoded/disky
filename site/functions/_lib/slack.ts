import { Memo, reqOf } from './shared.js'

/**
 * Slack Web API + request verification for the staged-deletion review loop
 * (specs/done/staged-slack.md). Pure fetch — no SDK. The bot token is the
 * deployment's Slack app ("CoreWeave Usage Bot" on cw); the signing secret
 * authenticates interactivity callbacks (`/slack/actions`).
 */

export interface SlackEnv {
  SLACK_BOT_TOKEN?: string
  SLACK_SIGNING_SECRET?: string
  /** Channel id staged-plan threads post to (e.g. #cw-s3-admin). Unset = no Slack. */
  SLACK_ADMIN_CHANNEL?: string
}

export const slackReady = (env: SlackEnv): boolean => !!(env.SLACK_BOT_TOKEN && env.SLACK_ADMIN_CHANNEL)

export interface SlackResult { ok: boolean; error?: string; ts?: string; channel?: string; [k: string]: unknown }

/** Call a Web API method with a JSON body. Never throws: `{ ok: false, error }`. */
export async function slackApi(env: SlackEnv, method: string, body: Record<string, unknown>): Promise<SlackResult> {
  if (!env.SLACK_BOT_TOKEN) return { ok: false, error: 'no SLACK_BOT_TOKEN' }
  try {
    const r = await fetch(`https://slack.com/api/${method}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    })
    const j = (await r.json().catch(() => ({ ok: false, error: `http ${r.status}` }))) as SlackResult
    if (!j.ok) console.log(`slack ${method} failed: ${j.error}${j.response_metadata ? ` ${JSON.stringify(j.response_metadata)}` : ''}`)
    return j
  } catch (e) {
    console.log(`slack ${method} threw: ${(e as Error).message}`)
    return { ok: false, error: (e as Error).message }
  }
}

const scopeCache = new Map<string, Promise<ReadonlySet<string>>>()

/** The bot token's granted scopes, as installed (Slack's `X-OAuth-Scopes` header on `auth.test`), once per token per
 *  isolate; empty when the call fails. What a post may rely on: `chat:write.customize` is what makes a `username`
 *  override show (without it Slack drops the override silently). */
export function slackScopes(env: SlackEnv): Promise<ReadonlySet<string>> {
  const token = env.SLACK_BOT_TOKEN
  if (!token) return Promise.resolve(new Set())
  let p = scopeCache.get(token)
  if (!p) {
    p = fetch('https://slack.com/api/auth.test', { method: 'POST', headers: { authorization: `Bearer ${token}` } })
      .then(r => new Set((r.headers.get('x-oauth-scopes') ?? '').split(',').map(x => x.trim()).filter(Boolean)))
      .catch(e => { console.log(`slack auth.test threw: ${(e as Error).message}`); return new Set<string>() })
    scopeCache.set(token, p)
  }
  return p
}

/** Forget the cached scopes (tests). */
export const resetSlackScopes = (): void => scopeCache.clear()

/** A Slack user's email (`users.info`, needs `users:read.email`), lowercased, or null. */
export async function slackUserEmail(env: SlackEnv, userId: string): Promise<string | null> {
  if (!env.SLACK_BOT_TOKEN) return null
  const r = await fetch(`https://slack.com/api/users.info?user=${encodeURIComponent(userId)}`, {
    headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` },
  })
  const j = (await r.json().catch(() => null)) as { ok?: boolean; user?: { profile?: { email?: string }; deleted?: boolean; is_bot?: boolean } } | null
  if (!j?.ok || j.user?.deleted || j.user?.is_bot) return null
  return j.user?.profile?.email?.toLowerCase() ?? null
}

const hex = (buf: ArrayBuffer): string => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')

/** Slack's v0 request signature: `v0=` HMAC-SHA256(secret, `v0:<ts>:<body>`),
 * with the timestamp within `maxSkew` seconds of `now` (replay window). */
export async function verifySlackSignature(
  secret: string, timestamp: string | null, signature: string | null, body: string,
  now: number = Math.floor(Date.now() / 1000), maxSkew = 300,
): Promise<boolean> {
  if (!timestamp || !signature || !/^\d+$/.test(timestamp)) return false
  if (Math.abs(now - Number(timestamp)) > maxSkew) return false
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`v0:${timestamp}:${body}`))
  const want = `v0=${hex(mac)}`
  if (want.length !== signature.length) return false
  let diff = 0
  for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ signature.charCodeAt(i)
  return diff === 0
}

/** A workspace member, as `users.lookupByEmail` returns them. */
export interface SlackPerson { name: string | null; image: string | null }

// email → member (or null: not in the workspace), per isolate; a lookup pending in another request is joined (`shared.ts`).
const personMemo = new Memo<SlackPerson | null>('slack-person')

/** Workspace members for `emails` (`users.lookupByEmail`; the bot has
 *  `users:read.email`). Unknown emails are left out. */
export async function slackPeople(env: SlackEnv, emails: readonly string[]): Promise<Record<string, SlackPerson>> {
  const out: Record<string, SlackPerson> = {}
  await Promise.all([...new Set(emails.map(e => e.toLowerCase()))].map(async e => {
    const v = await personMemo.get(e, () => (async () => {
        if (!env.SLACK_BOT_TOKEN) return null
        const r = await fetch(`https://slack.com/api/users.lookupByEmail?email=${encodeURIComponent(e)}`, { headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` } })
        const j = (await r.json().catch(() => null)) as { ok?: boolean; user?: { id?: string; deleted?: boolean; real_name?: string; profile?: { real_name?: string; image_192?: string } } } | null
        const u = j?.ok ? j.user : undefined
        return u?.id && !u.deleted ? { name: u.profile?.real_name || u.real_name || null, image: u.profile?.image_192 ?? null } : null
      })().catch(() => null), reqOf(env))
    if (v) out[e] = v
  }))
  return out
}
