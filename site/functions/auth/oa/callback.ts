/**
 * "Sign in with OA" callback (specs/oa-login-rp.md): verify state + nonce, trade the code for the broker's id_token,
 * verify it against the broker's JWKS, then `gate.signIn(email)` → app session → 302 to `?next`, as `/auth/google`'s
 * callback. A failed exchange (or the broker's `error=`, e.g. `login_required` for `prompt=none`) lands on
 * `/signin?error=oa:<reason>`.
 */
import { oidcCallback } from '@open-athena/auth/oidc'
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oaLoginConfig } from '../../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oaLoginConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('Not found\n', { status: 404 })
  // The broker's own refusal (`error=login_required` for a `prompt=none` without its session, `access_denied`, …):
  // the adapter would call it a missing code.
  const refused = new URL(ctx.request.url).searchParams.get('error')
  if (refused) return toSignIn(refused)
  const res = await oidcCallback(cfg)({ request: ctx.request })
  if (res.status >= 400) return toSignIn(res.headers.get('x-oidc-reason') ?? `HTTP ${res.status}`)
  return markDevSession(res, ctx.env)
}

const toSignIn = (why: string): Response => new Response(null, {
  status: 302,
  headers: { location: `/signin?error=${encodeURIComponent(`oa:${why}`)}`, 'cache-control': 'no-store' },
})
