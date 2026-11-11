/** `POST /api/logout`: drop the session cookie, and mark this browser signed out so the silent sign-in doesn't sign it
 *  straight back in through the broker's own session (`@disk-tree/pages-kit/oa` `markedSignedOut`). */
import { clearCookie, isSecureRequest } from '@open-athena/auth'
import { markedSignedOut } from '@disk-tree/pages-kit/oa'
import { type Ctx, oaConfig, SESSION_COOKIE } from '../_lib/env.js'

export const onRequestPost = (ctx: Ctx): Response => {
  const res = new Response(null, { status: 204, headers: { 'set-cookie': clearCookie({ name: SESSION_COOKIE, secure: isSecureRequest(ctx.request) }), 'cache-control': 'no-store' } })
  return markedSignedOut(ctx.request, oaConfig(ctx.env, ctx.request) !== null, res)
}
