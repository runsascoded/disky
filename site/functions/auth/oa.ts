/**
 * "Sign in with OA" start (specs/oa-login-rp.md): 302 to OA's broker (`OA_LOGIN_ISSUER`, login.oa.dev), which signs
 * the person in with Google (openathena.ai only) or, holding its own session, sends them straight back. The callback
 * (`oa/callback.ts`) mints the app session through the same gate as every other path. `?next`, `?prompt`,
 * `?account=choose` pass through as for `/auth/google`. Not configured: 404 (the wall shows no button).
 *
 * Without `prompt=none` this is a person asking to sign in, which ends any suppression of the silent attempt, a
 * deliberate sign-out's included (`_lib/silentSignIn.ts`). The handler is `@disk-tree/pages-kit/oa`'s, shared with
 * `fleet/`.
 */
import { oaStart } from '@disk-tree/pages-kit/oa'
import { type Ctx } from '../_lib/auth.js'
import { oaLoginConfig } from '../_lib/oidc.js'

export const onRequest = (ctx: Ctx): Promise<Response> => oaStart(oaLoginConfig(ctx.env, ctx.request), ctx.request)
