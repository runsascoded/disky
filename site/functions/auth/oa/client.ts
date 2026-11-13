/**
 * `GET /auth/oa/client` → `{ enabled, account?, google }`: whether "Sign in with OA" is configured here, so the wall
 * knows to render its button (specs/oa-login-rp.md); the account this browser last signed in as through it (`account`:
 * the signed `rememberAccount` cookie's address, with its profile's name and face), so the button can say "Continue as
 * …"; and the Google accounts it remembers (`google`, the adapter's hint cookies), so the wall can drop a Google button
 * that would only sign in the same account. Public: it says only that the path exists, and names nobody but the
 * account this browser proved it signed in as (the Google list is the browser's own cookie, echoed).
 */
import { hintedAccounts, rememberedAccount } from '@disk-tree/pages-kit/oa'
import { cookieNames, type Ctx, gateFor, json } from '../../_lib/auth.js'
import { oaLoginConfig } from '../../_lib/oidc.js'

export interface OaClient {
  enabled: boolean
  account?: { email: string, name?: string, avatar?: string }
  google: string[]
}

/** A face the wall can show as an `<img>` as-is: a stored one, or an https URL (never an `asset://` ref). */
const showable = (avatar?: string): string | undefined =>
  avatar && (avatar.startsWith('data:image/') || avatar.startsWith('https://')) ? avatar : undefined

export async function onRequestGet({ env, request }: Ctx): Promise<Response> {
  const names = cookieNames(env, request)
  const enabled = oaLoginConfig(env, request) !== null
  const body: OaClient = { enabled, google: hintedAccounts(request, names.accountHint) }
  const email = enabled && names.oaWho && env.SESSION_SECRET ? await rememberedAccount(request, names.oaWho, env.SESSION_SECRET) : null
  if (email) {
    const subject = await gateFor(env, request)?.subjectFor(email).catch(() => null)
    body.account = { email, ...(subject?.name ? { name: subject.name } : {}), ...(showable(subject?.avatar) ? { avatar: showable(subject?.avatar) } : {}) }
  }
  // Per browser now (its cookies), and a sign-in changes it: never reused.
  return json(body, 200, { 'cache-control': 'private, no-store' })
}
