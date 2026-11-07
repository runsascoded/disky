/**
 * "Sign in with OA" callback (specs/oa-login-rp.md): verify state + nonce, trade the code for the broker's id_token,
 * verify it against the broker's JWKS, then `gate.signIn(email)` → app session → 302 to `?next`, as `/auth/google`'s
 * callback.
 *
 * An explicit attempt's failure (the broker's `error=`, or the adapter's own) lands on `/signin?error=oa:<reason>`,
 * which says why. A silent one's (`_lib/silentSignIn.ts`) is no error: it lands on the wall, where they were, by way of
 * the adapter (`<next>?sso=<why>`), or on a quiet `/signin` or `/` when the adapter can't vouch for the state, as with
 * cookies blocked. `/signin` and `?sso=` never bounce again. The handler is `@disk-tree/pages-kit/oa`'s, shared with
 * `fleet/`.
 */
import { oaCallback } from '@disk-tree/pages-kit/oa'
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oaLoginConfig } from '../../_lib/oidc.js'

export const onRequest = (ctx: Ctx): Promise<Response> =>
  oaCallback(oaLoginConfig(ctx.env, ctx.request), ctx.request, res => markDevSession(res, ctx.env))
