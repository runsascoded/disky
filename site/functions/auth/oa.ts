/**
 * "Sign in with OA" start (specs/oa-login-rp.md): 302 to OA's broker (`OA_LOGIN_ISSUER`, login.oa.dev), which signs
 * the person in with Google (openathena.ai only) or, holding its own session, sends them straight back. The callback
 * (`oa/callback.ts`) mints the app session through the same gate as every other path. `?next`, `?prompt`,
 * `?account=choose` pass through as for `/auth/google`. Not configured: 404 (the wall shows no button).
 *
 * Without `prompt=none` this is a person asking to sign in, which ends any suppression of the silent attempt, a
 * deliberate sign-out's included (`_lib/silentSignIn.ts`).
 */
import { oidcStart } from '@open-athena/auth/oidc'
import { type Ctx } from '../_lib/auth.js'
import { oaLoginConfig } from '../_lib/oidc.js'
import { resumed } from '../_lib/silentSignIn.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oaLoginConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('Not found\n', { status: 404 })
  const res = await oidcStart(cfg)({ request: ctx.request })
  return new URL(ctx.request.url).searchParams.get('prompt') === 'none' ? res : resumed(ctx.request, res)
}
