/**
 * The package's whole `/api/auth/*` surface: whoami, exchange (`?key=` →
 * session), logout, request-access, and the admin grant/request/rotate/log
 * routes (minting — with its subject — happens here, via POST /api/auth/grants).
 * With `ADMIN_LIST` the gate's store refuses a grant carrying `admin` or `*`,
 * minted or edited in, and anything that gives one back its access (`grantStore`),
 * and with or without it a term that isn't a number; those refusals leave the
 * package's routes as a throw, answered here.
 */
import { authRoutes } from '@open-athena/auth'
import { d1AuditQuery } from '@open-athena/auth/d1'
import { ADMIN_SCOPE, AdminGrantRefused, BadGrantTerms, type Ctx, gateFor, json } from '../../_lib/auth.js'
import { siteAllowlist } from '../../_lib/allowlist.js'
import { markedSignedOut } from '../../_lib/silentSignIn.js'

/**
 * Local-dev auto sign-in. The package's admin routes authenticate by a signed
 * session cookie — they don't consult the app's localhost identity stub (which
 * only feeds `/data` etc.), so on `wrangler pages dev` the console would 401.
 * On localhost only, mint a `DEV_EMAIL` admin session and thread its cookie
 * through: into *this* request (so it authenticates immediately) and back out as
 * `Set-Cookie` (so the browser carries it on subsequent calls). Guarded by both
 * the hostname and `DEV_EMAIL`; Cloudflare routes by the real hostname, so a
 * gcs.oa.dev request's host is never `localhost` — this cannot fire in prod.
 */
const isLocalHost = (url: string): boolean => {
  const h = new URL(url).hostname
  return h === 'localhost' || h === '127.0.0.1'
}

/** Re-issue a request with the dev session cookie appended, preserving method
 *  and body (mint/rotate/revoke are POSTs). `pair` is the bare `name=value`. */
const withCookie = (req: Request, pair: string): Request => {
  const headers = new Headers(req.headers)
  const existing = headers.get('cookie')
  headers.set('cookie', existing ? `${existing}; ${pair}` : pair)
  const streamed = req.method !== 'GET' && req.method !== 'HEAD'
  return new Request(req.url, {
    method: req.method,
    headers,
    body: streamed ? req.body : undefined,
    ...(streamed ? { duplex: 'half' } : {}),
  } as RequestInit)
}

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const gate = gateFor(ctx.env, ctx.request)
  if (!gate) return new Response('auth backend not configured (DB / SESSION_SECRET)\n', { status: 503 })
  const handle = authRoutes(gate, {
    adminScope: ADMIN_SCOPE,
    audit: ctx.env.DB ? d1AuditQuery(ctx.env.DB) : undefined,
    // A share link minted with `allowlist: true` also lets its recipient sign
    // in (Google / emailed code): the `allowed_emails` row, via this adapter.
    allowlist: siteAllowlist(ctx.env),
  })

  let req = ctx.request
  let devCookie: string | undefined
  if (isLocalHost(ctx.request.url) && ctx.env.DEV_EMAIL && !(await gate.authenticate(ctx.request))) {
    const signed = await gate.signIn(ctx.env.DEV_EMAIL, ctx.request)
    if (signed) {
      devCookie = signed.cookie
      req = withCookie(ctx.request, signed.cookie.split(';')[0])
    }
  }

  let res: Response
  try {
    res = (await handle(req)) ?? new Response('not found\n', { status: 404 })
  } catch (e) {
    if (!(e instanceof AdminGrantRefused || e instanceof BadGrantTerms)) throw e
    res = json({ error: e.message }, e instanceof BadGrantTerms ? 400 : 403, { 'cache-control': 'no-store' })
  }
  // A deliberate sign-out: the broker's session outlives it, so no silent attempt until they ask (`_lib/silentSignIn.ts`).
  if (res.ok && ctx.request.method === 'POST' && new URL(ctx.request.url).pathname === '/api/auth/logout') res = markedSignedOut(ctx.request, ctx.env, res)
  if (!devCookie) return res
  const out = new Response(res.body, res)
  out.headers.append('set-cookie', devCookie)
  return out
}
