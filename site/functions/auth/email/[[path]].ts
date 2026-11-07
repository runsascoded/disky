/**
 * Email-code sign-in — the ZT One-Time-PIN replacement, alongside Google OIDC.
 * Mounted at `/auth/email/*` — deliberately NOT under the package's `/api/auth/*`
 * `authRoutes` catch-all, so there's no route-precedence ambiguity:
 *   POST /auth/email/start   -> email a magic link + 6-digit code (constant reply)
 *   POST /auth/email/code    -> verify the code, mint the session in this tab
 *   GET  /auth/email/verify  -> magic-link landing: mint + 302 to `next`
 *   GET  /auth/email/poll    -> original tab polls for the link being clicked
 * All converge on the same `gate.signIn` as Google — same D1 policy, sessions,
 * scopes. Dormant (503) until RESEND_API_KEY + MAIL_FROM are set.
 * See specs/done/oidc-cutover-cw.md.
 */
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { emailCodeFor } from '../../_lib/emailcode.js'

/** Pages passes the full EventContext, which carries `waitUntil` (used to keep
 *  `start`'s response latency constant regardless of send outcome). */
type EmailCtx = Ctx & { waitUntil?: (p: Promise<unknown>) => void }

export const onRequest = async (ctx: EmailCtx): Promise<Response> => {
  const handlers = emailCodeFor(ctx.env, ctx.request)
  if (!handlers) {
    return new Response('email-code auth not configured (DB / SESSION_SECRET / RESEND_API_KEY / MAIL_FROM)\n', { status: 503 })
  }
  const { request } = ctx
  const seg = new URL(request.url).pathname.split('/').pop()
  switch (seg) {
    case 'start': return handlers.start({ request, waitUntil: ctx.waitUntil })
    case 'code': return markDevSession(await handlers.verifyCode({ request }), ctx.env)
    case 'verify': {
      const res = await handlers.verifyLink({ request })
      // A dead link (expired, already used, unknown) bounces to `next` with no
      // session — which just re-shows the wall with no explanation. Say why on
      // the sign-in page instead. A policy denial keeps the adapter's own
      // `?denied=<email>` bounce (the wall unfolds request-access for it).
      const loc = res.headers.get('location') ?? '/'
      if (res.status === 302 && !res.headers.get('set-cookie') && !loc.includes('?denied=')) {
        return new Response(null, {
          status: 302,
          headers: { location: `/signin?error=link&next=${encodeURIComponent(loc)}`, 'cache-control': 'no-store' },
        })
      }
      return markDevSession(res, ctx.env)
    }
    case 'poll': return handlers.poll({ request })
    default: return new Response('not found\n', { status: 404 })
  }
}
