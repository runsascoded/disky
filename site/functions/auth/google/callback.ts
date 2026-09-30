/**
 * OIDC callback: verify state + nonce, trade the code for an id_token, verify
 * it against Google's JWKS, then `gate.signIn(email)` → app session cookie →
 * 302 back to `?next`. Authenticated-but-not-admitted (not staff, not a viewer
 * domain, no `allowed_emails` row) 302s to `/?denied=<email>`. This is the
 * whole ZT-free identity path; everything downstream (gate, D1, sessions,
 * share links) is unchanged. See specs/oidc-cutover-cw.md.
 */
import { oidcCallback } from '@open-athena/auth/oidc'
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oidcConfig } from '../../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oidcConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('OIDC not configured\n', { status: 503 })
  const res = await oidcCallback(cfg)({ request: ctx.request })
  // The adapter answers a failed exchange with a bare-text 400 (reason in
  // `x-oidc-reason`: replayed/expired state, nonce mismatch, token exchange…).
  // Land on the sign-in page with the reason instead, so the person sees the
  // app's chrome, the explanation, and the buttons to try again.
  if (res.status >= 400) {
    const why = res.headers.get('x-oidc-reason') ?? `HTTP ${res.status}`
    return new Response(null, {
      status: 302,
      headers: { location: `/signin?error=${encodeURIComponent(`google:${why}`)}`, 'cache-control': 'no-store' },
    })
  }
  return markDevSession(res, ctx.env)
}
