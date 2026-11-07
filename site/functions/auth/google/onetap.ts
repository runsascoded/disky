/**
 * `POST /auth/google/onetap` `{ credential, nonce }`: verify the id_token
 * Google's in-page button produced (same JWKS verification as the redirect
 * callback, plus the nonce binding) and sign the session into *this* response
 * — the page is already where it wants to be. A verified address that isn't
 * on the allowlist answers `401 { denied }`, and the wall unfolds request-access
 * pre-filled with it.
 */
import { googleOneTapVerify } from '@open-athena/auth/oidc'
import { type Ctx, gateFor } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const gate = gateFor(ctx.env)
  const clientId = ctx.env.GOOGLE_CLIENT_ID
  if (!gate || !clientId) return new Response('Google sign-in not configured\n', { status: 503 })
  const res = await googleOneTapVerify({ gate, clientId })({ request: ctx.request })
  return markDevSession(res, ctx.env)
}
