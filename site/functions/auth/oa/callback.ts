/**
 * "Sign in with OA" callback (specs/oa-login-rp.md): verify state + nonce, trade the code for the broker's id_token,
 * verify it against the broker's JWKS, then `gate.signIn(email)` → app session → 302 to `?next`, as `/auth/google`'s
 * callback.
 *
 * An explicit attempt's failure (the broker's `error=`, or the adapter's own) lands on `/signin?error=oa:<reason>`,
 * which says why. A silent one's (`_lib/silentSignIn.ts`) is no error: it lands on the wall, where they were, by way of
 * the adapter (`<next>?sso=<why>`), or on a quiet `/signin` or `/` when the adapter can't vouch for the state, as with
 * cookies blocked. `/signin` and `?sso=` never bounce again. The handler is `@disk-tree/pages-kit/oa`'s, shared with
 * `fleet/`. A completed one also leaves the signed remembered account (`rememberAccount`) the wall's button names.
 */
import { oaCallback, rememberAccount } from '@disk-tree/pages-kit/oa'
import { cookieNames, type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oaLoginConfig } from '../../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { env, request } = ctx
  const { oaHint, oaWho } = cookieNames(env, request)
  const res = await oaCallback(oaLoginConfig(env, request), request, r => markDevSession(r, env))
  // A completed sign-in (the adapter wrote the hint): remember who, signed, for the wall's "Continue as …".
  return oaHint && oaWho && env.SESSION_SECRET && res.status < 400
    ? rememberAccount(res, request, { hintCookie: oaHint, whoCookie: oaWho, secret: env.SESSION_SECRET })
    : res
}
