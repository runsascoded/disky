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
    if (!j.ok) console.log(`slack ${method} failed: ${j.error}`)
    return j
  } catch (e) {
    console.log(`slack ${method} threw: ${(e as Error).message}`)
    return { ok: false, error: (e as Error).message }
  }
}

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
