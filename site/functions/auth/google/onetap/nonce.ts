/**
 * `GET /auth/google/onetap/nonce` → `{ nonce }`: an HMAC-signed, short-lived
 * nonce the page feeds to Google's button; the id_token comes back carrying
 * it, and `/auth/google/onetap` accepts only a nonce we minted. Storage-free
 * replay defence, same trick as the redirect flow's `state`.
 */
import { googleOneTapNonce } from '@open-athena/auth/oidc'
import { type Ctx, gateFor } from '../../../_lib/auth.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gate = gateFor(ctx.env)
  if (!gate) return new Response('auth not configured (DB / SESSION_SECRET)\n', { status: 503 })
  return googleOneTapNonce({ gate })()
}
