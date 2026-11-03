/**
 * "Sign in with OA" start (specs/oa-login-rp.md): 302 to OA's broker (`OA_LOGIN_ISSUER`, login.oa.dev), which signs
 * the person in with Google (openathena.ai only) or, holding its own session, sends them straight back. The callback
 * (`oa/callback.ts`) mints the app session through the same gate as every other path. `?next`, `?prompt`,
 * `?account=choose` pass through as for `/auth/google`. Not configured: 404 (the wall shows no button).
 */
import { oidcStart } from '@open-athena/auth/oidc'
import { type Ctx } from '../_lib/auth.js'
import { oaLoginConfig } from '../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oaLoginConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('Not found\n', { status: 404 })
  return oidcStart(cfg)({ request: ctx.request })
}
