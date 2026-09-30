/**
 * OIDC sign-in start: 302 to Google's consent screen (our own OAuth client),
 * the ZT-free replacement for the CF Access edge gate. The callback
 * (`google/callback.ts`) mints the app session, so the gate, D1 policy, scopes,
 * and share links are all the same as every other identity path. Reads `?next`
 * for where to land after. Dormant until the Google client secrets are set —
 * see specs/oidc-cutover-cw.md.
 */
import { oidcStart } from '@open-athena/auth/oidc'
import { type Ctx } from '../_lib/auth.js'
import { oidcConfig } from '../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oidcConfig(ctx.env, ctx.request)
  if (!cfg) {
    return new Response('OIDC not configured (DB / SESSION_SECRET / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)\n', { status: 503 })
  }
  return oidcStart(cfg)({ request: ctx.request })
}
