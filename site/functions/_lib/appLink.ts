/**
 * The macOS app's sign-in hand-off (specs/app-link.md). Google refuses OAuth
 * inside the app's WKWebView, so the person signs in here in the system
 * browser and "Open in disky" hands the app a one-time credential:
 *
 *   GET  /auth/app-handoff        (browser, opened by the app's sign-in wall:
 *                                  signs in if needed, then a tiny page —
 *                                  no SPA — that hands `url` to the app)
 *   POST /api/app-link            (the SPA's "Open in disky") → { url }
 *   disky://open?link=<url>       (the browser hands `url` to the app)
 *   GET  /auth/app-link?token=…   (the app's webview) → session cookie, 303 → next
 *
 * The link is an `@open-athena/auth` grant bound to the caller's own email,
 * carrying the caller's own scopes, `maxRedeems: 1`, expiring `APP_LINK_TTL_S`
 * after mint. Redeeming it spends that one redemption (the store's atomic
 * `UPDATE … WHERE redeems < max_redeems RETURNING`), revokes the grant (so the
 * raw token, which the gate would otherwise accept as a request-scoped
 * `?key=`/Bearer credential until expiry, dies with its redemption), and signs
 * the webview in as that email — an ordinary email session, exactly what a
 * browser sign-in mints, whose scopes the policy re-derives on every request.
 * Not a grant session: that would render as a "guest share link", carry a
 * scope snapshot, and end with the grant.
 */
import { type AccessEventKind, hashToken, requestMeta } from '@open-athena/auth'
import { d1AuditSink, d1GrantStore } from '@open-athena/auth/d1'
import { type Ctx, gateFor, identify, json } from './auth.js'
import { markDevSession } from './devsession.js'

/**
 * How long a minted link stays redeemable. The hop it has to survive: the
 * browser's "Open disky?" confirmation (a human click — the dominant term),
 * the app's cold launch, and one webview navigation. A minute covers all
 * three with margin; anyone slower clicks the button again.
 */
export const APP_LINK_TTL_S = 60
/** The grant's name: how `/auth/app-link` recognizes a link it may redeem
 *  (with the self-mint shape — see `isAppLink`), and how it reads in the
 *  admin grants list. */
export const APP_LINK_NAME = 'disky app sign-in'
/** The audit-log `event` for this flow's own rows (`reason`: `mint` | `redeem`),
 *  beside the gate's generic `mint` / `redeem` / `revoke` / `signin` rows. */
export const APP_LINK_EVENT = 'app-link'
export const REDEEM_PATH = '/auth/app-link'
export const HANDOFF_PATH = '/auth/app-handoff'

interface Grantish {
  name: string | null
  email: string | null
  createdBy: string
  maxRedeems: number | null
  createdAt: number
  expiresAt: number | null
}

/** Only a self-minted, single-use, short-lived link converts to an email
 *  session here. An admin's share link — even one bound to an email — never
 *  does (it would turn a read-only guest grant into a full sign-in), and is
 *  left unspent. */
export const isAppLink = (g: Grantish): boolean =>
  g.name === APP_LINK_NAME
  && g.email !== null
  && g.createdBy === g.email
  && g.maxRedeems === 1
  && g.expiresAt !== null
  && g.expiresAt - g.createdAt <= APP_LINK_TTL_S

/** A same-origin path to land on after redeeming; anything else goes home. */
export const safeNext = (raw: unknown): string =>
  typeof raw === 'string' && raw.startsWith('/') && !raw.startsWith('//') && !raw.includes('\\') && raw.length <= 2048 ? raw : '/'

/**
 * A browser-initiated, same-origin request? `Origin` must be present and equal
 * this origin (browsers send it on every POST), and `Sec-Fetch-Site`, where
 * sent, must say `same-origin` — so another site can't make a signed-in
 * visitor's browser mint a link (a cross-site form POST carries a foreign or
 * `null` Origin).
 */
export function sameOrigin(req: Request): boolean {
  const origin = req.headers.get('origin')
  if (!origin || origin !== new URL(req.url).origin) return false
  const site = req.headers.get('sec-fetch-site')
  return site === null || site === 'same-origin'
}

const sec = (ms: number) => Math.floor(ms / 1000)

async function audit(ctx: Ctx, nowMs: number, e: { grantId: string; email: string; reason: 'mint' | 'redeem' }) {
  if (!ctx.env.DB || !ctx.env.SESSION_SECRET) return
  const meta = await requestMeta(ctx.request, ctx.env.SESSION_SECRET)
  await d1AuditSink(ctx.env.DB).log({
    ...meta,
    ts: sec(nowMs),
    // A distinct kind, so the hand-off is one filter away in the log; the
    // gate's union doesn't name it, but the column is free text.
    event: APP_LINK_EVENT as AccessEventKind,
    grantId: e.grantId,
    sessionSub: `e:${e.email}`,
    reason: e.reason,
  })
}

const noStore = { 'cache-control': 'no-store', 'referrer-policy': 'no-referrer' }

/** `POST /api/app-link` `{ next? }` → `{ url, expires_at }`. */
export async function mintAppLink(ctx: Ctx, nowMs = Date.now()): Promise<Response> {
  const { request: req } = ctx
  if (req.method !== 'POST') return json({ error: 'method not allowed' }, 405, { allow: 'POST' })
  if (!sameOrigin(req)) return json({ error: 'cross-site request refused' }, 403)
  const gate = gateFor(ctx.env)
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  // Only a signed-in person hands off their own sign-in. A grant (share link,
  // agent token, or a previous hand-off's raw token) minting a grant would be
  // a link laundering itself into a fresh one.
  if (id.via !== 'session' || !id.email) return json({ error: 'only a signed-in session can open the app' }, 403)
  if (!gate) return json({ error: 'auth backend not configured (DB / SESSION_SECRET)' }, 503)
  let next = '/'
  if ((req.headers.get('content-type') ?? '').includes('application/json')) {
    const body = await req.json().catch(() => null) as { next?: unknown } | null
    next = safeNext(body?.next)
  }
  const { url, expiresAt } = await mint(ctx, gate, id, next, nowMs)
  return json({ url, expires_at: expiresAt }, 200, noStore)
}

type Gate = NonNullable<ReturnType<typeof gateFor>>

/** Mint `id`'s link (a signed-in session's, checked by the caller). */
async function mint(
  ctx: Ctx,
  gate: Gate,
  id: { email: string | null; scopes: Iterable<string> },
  next: string,
  nowMs: number,
): Promise<{ url: string; expiresAt: number | null }> {
  const email = (id.email as string).toLowerCase()
  const nowS = sec(nowMs)
  const { grant, token } = await gate.mint({
    name: APP_LINK_NAME,
    note: 'self-minted by "Open in disky": a single-use hand-off to the macOS app',
    email,
    // The caller's own scopes, as the gate resolved them for this request —
    // never more (the session the link becomes re-derives them anyway).
    scopes: [...id.scopes],
    maxRedeems: 1,
    expiresAt: nowS + APP_LINK_TTL_S,
    createdBy: email,
  }, nowMs)
  await audit(ctx, nowMs, { grantId: grant.id, email, reason: 'mint' })
  const url = new URL(REDEEM_PATH, ctx.request.url)
  url.searchParams.set('token', token)
  if (next !== '/') url.searchParams.set('next', next)
  return { url: url.toString(), expiresAt: grant.expiresAt }
}

const esc = (t: string) => t.replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`)

/** The hand-off tab's whole page: no SPA. It opens the app (the button is the
 *  fallback, and a user gesture where a browser wants one), then closes once
 *  the app has taken focus — closing in the same tick as the `disky://`
 *  navigation cancels Chrome's launch. */
function handoffHtml(body: string, appUrl?: string): string {
  const go = appUrl ? `<p><a class="btn" href="${esc(appUrl)}">Open disky</a></p>
<script>
location.href = ${JSON.stringify(appUrl).replace(/</g, '\\u003c')}
const close = () => setTimeout(() => window.close(), 1000)
if (!document.hasFocus()) close(); else addEventListener('blur', close, { once: true })
</script>` : ''
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>disky sign-in</title>
<style>
:root { color-scheme: light dark; --bg: #f6f6f4; --ink: #1d1d1b; --mute: #6b6b66; --acc: #2f6fde }
@media (prefers-color-scheme: dark) { :root { --bg: #1b1b1b; --ink: #ececea; --mute: #a3a39e; --acc: #3d7ee8 } }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink); font: 16px/1.5 system-ui, sans-serif }
main { max-width: 420px; padding: 32px 16px; text-align: center }
h1 { font-size: 22px; margin: 0 0 8px } p { color: var(--mute); margin: 8px 0 }
.btn { display: inline-block; margin-top: 8px; padding: 9px 18px; border-radius: 6px; background: var(--acc); color: #fff; text-decoration: none; font-weight: 600 }
</style></head>
<body><main>${body}${go}</main></body></html>
`
}

const html = (body: string, status = 200) =>
  new Response(body, { status, headers: { 'content-type': 'text/html; charset=utf-8', ...noStore } })

/**
 * `GET /auth/app-handoff[?next=/path]` — the macOS app's sign-in wall opens
 * this in the default browser. No session → Google sign-in, returning here. A
 * session → mint its link and hand it to the app from a tiny page (never the
 * SPA). Minting on a GET is gated on `Sec-Fetch-Site`: `none` (the OS opened
 * the tab) or `same-origin` (back from the sign-in redirect); a cross-site
 * navigation gets a button to click instead, so another site can't make a
 * visitor's browser mint one unprompted.
 */
export async function appHandoff(ctx: Ctx, nowMs = Date.now()): Promise<Response> {
  const { request: req } = ctx
  if (req.method !== 'GET') return json({ error: 'method not allowed' }, 405, { allow: 'GET' })
  const params = new URL(req.url).searchParams
  const next = safeNext(params.get('next'))
  const self = next === '/' ? HANDOFF_PATH : `${HANDOFF_PATH}?next=${encodeURIComponent(next)}`
  const gate = gateFor(ctx.env)
  if (!gate) return html(handoffHtml('<h1>Sign-in unavailable</h1><p>The auth backend isn’t configured.</p>'), 503)
  const id = await identify(ctx)
  if (!id || id.via !== 'session' || !id.email) {
    return new Response(null, { status: 303, headers: { location: `/auth/google?next=${encodeURIComponent(self)}`, ...noStore } })
  }
  const site = req.headers.get('sec-fetch-site')
  if (site !== null && site !== 'none' && site !== 'same-origin') {
    return html(handoffHtml(`<h1>Open disky?</h1><p>Signed in as ${esc(id.email)}.</p><p><a class="btn" href="${esc(self)}">Continue</a></p>`))
  }
  const { url } = await mint(ctx, gate, id, next, nowMs)
  const appUrl = `disky://open?link=${encodeURIComponent(url)}`
  return html(handoffHtml(`<h1>Signed in to disky</h1><p>As ${esc(id.email)}. You can close this tab.</p>`, appUrl))
}

const fail = (reason: string): Response =>
  new Response(null, { status: 303, headers: { location: `/signin?error=${encodeURIComponent(`app-link:${reason}`)}`, ...noStore } })

/** `GET /auth/app-link?token=…[&next=/path]` → session cookie + 303 to `next`;
 *  a dead link 303s to `/signin?error=app-link:<reason>`. */
export async function redeemAppLink(ctx: Ctx, nowMs = Date.now()): Promise<Response> {
  const { request: req, env } = ctx
  if (req.method !== 'GET') return json({ error: 'method not allowed' }, 405, { allow: 'GET' })
  const gate = gateFor(env)
  if (!gate || !env.DB) return new Response('auth backend not configured (DB / SESSION_SECRET)\n', { status: 503 })
  const params = new URL(req.url).searchParams
  const token = params.get('token')
  if (!token) return fail('bad-token')
  // Look before spending: a token that isn't an app link (an admin's share
  // link pasted here) is refused without burning one of its redemptions.
  const found = await d1GrantStore(env.DB).byTokenHash(await hashToken(token))
  if (!found || !isAppLink(found)) return fail('bad-token')
  const res = await gate.redeem(token, req, nowMs)
  if (!res.ok) return fail(res.reason)
  const { grant } = res
  await gate.revoke(grant.id, nowMs)
  const email = grant.email as string
  // Through the policy, like any sign-in: an address removed since the mint
  // gets nothing.
  const signed = await gate.signIn(email, req, nowMs)
  if (!signed) return fail('not-allowed')
  await audit(ctx, nowMs, { grantId: grant.id, email, reason: 'redeem' })
  return markDevSession(new Response(null, {
    status: 303,
    headers: { location: safeNext(params.get('next')), 'set-cookie': signed.cookie, ...noStore },
  }), env)
}
