/**
 * Shared config for the email-code sign-in Functions (`/auth/email/*`) — the
 * ZT One-Time-PIN replacement, alongside Google OIDC. Reuses the same gate /
 * D1 policy / session as every other identity path; the only new pieces are
 * the `pending_auth` store (migrations/cw 0015–0016) and a Resend sender.
 * Dormant until RESEND_API_KEY + MAIL_FROM are set. See
 * specs/done/oidc-cutover-cw.md.
 */
import { emailCodeAuth } from '@open-athena/auth'
import { d1PendingAuthStore } from '@open-athena/auth/d1'
import { resendEmail } from '@open-athena/auth/resend'
import { type Env, gateFor } from './auth.js'

export type EmailCodeHandlers = ReturnType<typeof emailCodeAuth>

/** The four email-code handlers, or null if the deployment isn't configured
 *  for it (no gate — DB/SESSION_SECRET — or no Resend sender). */
export function emailCodeFor(env: Env, request: Request): EmailCodeHandlers | null {
  const gate = gateFor(env)
  if (!gate || !env.DB || !env.RESEND_API_KEY || !env.MAIL_FROM) return null
  const origin = new URL(request.url).origin
  return emailCodeAuth({
    gate,
    store: d1PendingAuthStore(env.DB),
    send: resendEmail({ apiKey: env.RESEND_API_KEY, from: env.MAIL_FROM }),
    from: env.MAIL_FROM,
    // The magic link lands on our own verify route (same origin as the request).
    linkFor: (token) => `${origin}/auth/email/verify?token=${encodeURIComponent(token)}`,
    appName: env.ROOT_LABEL ?? 'storage usage',
  })
}
