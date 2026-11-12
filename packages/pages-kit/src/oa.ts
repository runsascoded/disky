/**
 * "Sign in with OA" for a Pages app (specs/oa-login-rp.md): OA's broker (`OA_LOGIN_ISSUER`, login.oa.dev) as the OIDC
 * provider for an `@open-athena/auth` gate, its start and callback routes, and the silent sign-in that sends a person's
 * unauthenticated page visit to the broker once before the wall. Shared by `site/` (`functions/_lib/oidc.ts`,
 * `_lib/silentSignIn.ts`, `auth/oa*.ts`) and `fleet/`: each app supplies its gate and what it walls.
 *
 * Silent sign-in: the broker holding a session sends the person straight back signed in; otherwise they land where
 * they were with `?sso=<why>`, and the app shows its wall. Nothing here reads storage: a session is judged by the app's
 * `presents` (its signature), so a signed-in visit pays nothing.
 *
 * `SKIP_COOKIE` is what stops a repeat, with one of two values:
 * - `attempt` (`ATTEMPT_TTL_S`): this browser just tried. Short, so a sign-in at the broker elsewhere still takes on a
 *   later visit, and a person with no broker session sees at most one bounce per five minutes.
 * - `signout` (`SIGNED_OUT_TTL_S`): this browser deliberately signed out. The broker's session survives an app logout
 *   (by design: `oa/auth`'s `specs/cross-service-sso.md`), so an attempt would sign them straight back in. It lasts until
 *   they resume: a click on "Sign in with OA" (`/auth/oa` without `prompt=none`), or any completed sign-in
 *   (`clearedOnSignIn`).
 *
 * With cookies blocked the `?sso=` marker alone stops the second attempt on the way back. A reload is then one more
 * bounce, never a loop.
 */
import { clearCookie, type Gate, isSecureRequest, readCookie, sessionCookie, signSession, verifySession } from '@open-athena/auth'
import { oidcCallback, type OidcOptions, oidcStart, redirectFromOrigin } from '@open-athena/auth/oidc'

/** The vars that configure the broker: the issuer and a client (any unset = not configured). The preview pair is the
 *  deployment's preview client at the broker (`<client id>-preview`, its callback the one-label pattern
 *  `https://*.<project subdomain>/auth/oa/callback`), set only in the Pages preview environment: the only client a
 *  preview host (`OaLoginHost.preview`) uses. */
export interface OaLoginEnv {
  OA_LOGIN_ISSUER?: string
  OA_LOGIN_CLIENT_ID?: string
  OA_LOGIN_CLIENT_SECRET?: string
  OA_LOGIN_PREVIEW_CLIENT_ID?: string
  OA_LOGIN_PREVIEW_CLIENT_SECRET?: string
}

export interface OidcCfg {
  gate: Gate
  clientId: string
  clientSecret: string
  redirectUri: OidcOptions['redirectUri']
}

export type OaLoginCfg = OidcCfg & Required<Pick<OidcOptions, 'provider' | 'nonceCookieName' | 'accountHintCookie'>>

/** What the app knows about the request's host. This module doesn't classify hosts; the app does. */
export interface OaLoginHost {
  /** A PR preview's branch host: the preview client with a redirect on the request's own origin, or nothing. */
  preview?: boolean
  /** The nonce cookie's name (default `OA_NONCE_COOKIE`), e.g. a `__Host-` one on https. */
  nonceCookieName?: string
  /** The adapter's account-hint cookie for broker sign-ins (e.g. `OA_HINT_COOKIE`): what `rememberAccount` reads the
   *  address from, and the `login_hint` a later attempt sends. Default none. */
  accountHintCookie?: string | false
}

export const OA_CALLBACK_PATH = '/auth/oa/callback'
export const OA_NONCE_COOKIE = 'oa_login_nonce'

/** The adapter config for "Sign in with OA" (`/auth/oa` + its callback): OA's broker as the OIDC provider, or null where
 *  it isn't configured (no gate, or `OA_LOGIN_ISSUER` or the host's client id / secret unset).
 *  No `hd`: the broker admits only its Workspace; the gate's policy decides admission as for every other path. Its own
 *  nonce cookie (a Google round trip in another tab keeps its own) and its own account-hint cookie where the app names
 *  one (`host.accountHintCookie`, never Google's: those feed Google's chooser), which `rememberAccount` reads.
 *
 *  On a preview host (`host.preview`) the client is the preview pair alone, with `redirectFromOrigin`: the broker checks
 *  the concrete callback against that client's one-label pattern. Without the pair it is null (no button, 404), never
 *  the deployment's own client. */
export function oaLoginOptions(gate: Gate | null, env: OaLoginEnv, request: Request, host: OaLoginHost = {}): OaLoginCfg | null {
  const issuer = env.OA_LOGIN_ISSUER?.replace(/\/+$/, '')
  if (!gate || !issuer) return null
  const [clientId, clientSecret] = host.preview
    ? [env.OA_LOGIN_PREVIEW_CLIENT_ID, env.OA_LOGIN_PREVIEW_CLIENT_SECRET]
    : [env.OA_LOGIN_CLIENT_ID, env.OA_LOGIN_CLIENT_SECRET]
  if (!clientId || !clientSecret) return null
  const redirectUri = host.preview ? redirectFromOrigin(OA_CALLBACK_PATH) : `${new URL(request.url).origin}${OA_CALLBACK_PATH}`
  const client = { clientId, clientSecret, redirectUri }
  return {
    gate,
    ...client,
    provider: { authUrl: `${issuer}/oidc/authorize`, tokenUrl: `${issuer}/oidc/token`, jwksUrl: `${issuer}/oidc/jwks`, issuer, scope: 'openid email profile' },
    nonceCookieName: host.nonceCookieName ?? OA_NONCE_COOKIE,
    accountHintCookie: host.accountHintCookie ?? false,
  }
}

export const OA_HINT_COOKIE = 'oa_login_hint'
export const OA_WHO_COOKIE = 'oa_login_who'
/** The signed remembered account's subject prefix: never `e:`/`g:`, so the value is inert as a session. */
const WHO_PREFIX = 'who:'

/** `res` (a completed broker sign-in) plus a signed `whoCookie` naming the address the adapter just wrote to
 *  `hintCookie`. The hint is client-controlled on later requests; this is what the wall trusts to show a name and face
 *  ("Continue as …"), so only a browser that signed in as an address is shown that address's profile. Like Google's
 *  hints it outlives sign-out: it says who was here last, never who is here. */
export async function rememberAccount(
  res: Response,
  request: Request,
  { hintCookie, whoCookie, secret }: { hintCookie: string, whoCookie: string, secret: string },
): Promise<Response> {
  const set = res.headers.getSetCookie().find(c => c.startsWith(`${hintCookie}=`))
  const raw = set?.slice(hintCookie.length + 1).split(';')[0]
  if (!raw) return res
  let email: string
  try { email = decodeURIComponent(raw).trim().toLowerCase() } catch { return res }
  if (!email.includes('@')) return res
  const value = await signSession(`${WHO_PREFIX}${email}`, secret, Date.now(), SIGNED_OUT_TTL_S)
  return withCookie(res, sessionCookie(value, { name: whoCookie, ttlS: SIGNED_OUT_TTL_S, secure: isSecureRequest(request) }))
}

/** The address `rememberAccount` signed for this browser, or null (none, forged, expired). */
export async function rememberedAccount(request: Request, whoCookie: string, secret: string): Promise<string | null> {
  const raw = readCookie(request, whoCookie)
  if (!raw) return null
  const sub = await verifySession(raw, secret, Date.now())
  return sub?.startsWith(WHO_PREFIX) ? sub.slice(WHO_PREFIX.length) : null
}

/** The addresses an adapter's account-hint cookie pair (`name`, `name_accounts`) remembers, newest first, as the
 *  adapter reads them: presentation only, never an identity. */
export function hintedAccounts(request: Request, name: string | false): string[] {
  if (!name) return []
  const out: string[] = []
  const add = (v: unknown) => {
    if (typeof v !== 'string') return
    const email = v.trim().toLowerCase()
    if (email.includes('@') && email.length <= 254 && !out.includes(email)) out.push(email)
  }
  const decode = (raw: string | null): unknown => {
    if (!raw || raw.length > 3000) return null
    try { return decodeURIComponent(raw) } catch { return null }
  }
  add(decode(readCookie(request, name)))
  const list = decode(readCookie(request, `${name}_accounts`))
  if (typeof list === 'string') {
    try {
      const parsed: unknown = JSON.parse(list)
      if (Array.isArray(parsed)) parsed.forEach(add)
    } catch {
      // Malformed client-controlled history is an absent hint, not an error.
    }
  }
  return out.slice(0, 5)
}

export const SKIP_COOKIE = 'oa_sso_skip'
export const ATTEMPT_TTL_S = 5 * 60
/** Chrome caps cookie lifetimes at 400 days: as long as one can last, not a judgement about how long is enough. */
export const SIGNED_OUT_TTL_S = 400 * 24 * 60 * 60

const skipCookie = (request: Request, value: 'attempt' | 'signout', ttlS: number): string =>
  sessionCookie(value, { name: SKIP_COOKIE, ttlS, secure: isSecureRequest(request) })

/** `res` (a copy: a route's headers may be immutable) with one more `Set-Cookie`. */
function withCookie(res: Response, cookie: string): Response {
  const out = new Response(res.body, res)
  out.headers.append('set-cookie', cookie)
  return out
}

/** A person's top-level page load, by what browsers alone send and a page can't forge. A crawler or unfurler sends no
 *  `Sec-Fetch-*`, and neither does an older browser: no header, no attempt. A prefetch or prerender isn't the person yet. */
const isDocumentNavigation = (request: Request): boolean => {
  const h = request.headers
  return request.method === 'GET'
    && h.get('sec-fetch-mode') === 'navigate' && h.get('sec-fetch-dest') === 'document'
    && !h.has('sec-purpose') && !h.has('purpose')
}

/** A visit that goes its own way: a share link's exchange (`key`), a preview link (`og`), the wall's request-access
 *  form (`denied`), or one this attempt already answered (`sso`). */
const OWN_WAY = ['key', 'og', 'denied', 'sso']

/** What an app tells the silent attempt, each asked only when the cheaper checks before it haven't decided. */
export interface SilentApp {
  /** Paths a document navigation can reach that aren't walled pages. */
  notWalled: RegExp
  /** Nothing is walled here at all (a public deployment). */
  anonymous: () => boolean
  /** The broker is configured (`oaLoginOptions` non-null). */
  configured: () => boolean
  /** The request already carries a credential that passes. */
  presents: () => Promise<boolean>
}

/** The redirect to the broker for this request, or null to serve it as ever. */
export async function silentAttempt(request: Request, app: SilentApp): Promise<Response | null> {
  if (!isDocumentNavigation(request)) return null
  const url = new URL(request.url)
  // `//host` is what `next` refuses (the adapter's `safeNext` would send them home): the wall, where they asked to be.
  if (app.notWalled.test(url.pathname) || url.pathname.startsWith('//')) return null
  if (OWN_WAY.some(k => url.searchParams.has(k))) return null
  if (readCookie(request, SKIP_COOKIE) || request.headers.has('authorization')) return null
  if (app.anonymous() || !app.configured()) return null
  if (await app.presents()) return null
  const next = encodeURIComponent(url.pathname + url.search)
  return new Response(null, {
    status: 302,
    headers: { location: `/auth/oa?prompt=none&next=${next}`, 'cache-control': 'no-store', 'set-cookie': skipCookie(request, 'attempt', ATTEMPT_TTL_S) },
  })
}

/** Whether this callback answers a silent attempt: the browser's marker is still here (an explicit `/auth/oa` clears
 *  it, `resumed`). */
export const attemptInFlight = (request: Request): boolean => readCookie(request, SKIP_COOKIE) === 'attempt'

/** Logout's response, this browser marked no-attempt until it resumes. Unconfigured: `res` itself. */
export const markedSignedOut = (request: Request, configured: boolean, res: Response): Response =>
  configured ? withCookie(res, skipCookie(request, 'signout', SIGNED_OUT_TTL_S)) : res

/** An explicit `/auth/oa` start's response, the no-attempt state cleared: the person has asked to sign in. */
export const resumed = (request: Request, res: Response): Response =>
  readCookie(request, SKIP_COOKIE) ? withCookie(res, clearCookie({ name: SKIP_COOKIE, secure: isSecureRequest(request) })) : res

/** Any route's response: one minting the session `sessionCookieName` (every sign-in path converges on it) also clears
 *  the no-attempt state. Everything else, and everything on an unconfigured deployment, is `res` itself. */
export function clearedOnSignIn(request: Request, sessionCookieName: string, configured: () => boolean, res: Response): Response {
  if (!readCookie(request, SKIP_COOKIE)) return res
  const name = `${sessionCookieName}=`
  const minted = res.headers.getSetCookie().some(c => c.startsWith(name) && !c.startsWith(`${name};`))
  return minted && configured() ? resumed(request, res) : res
}

const notFound = (): Response => new Response('Not found\n', { status: 404 })

/** `/auth/oa`: 302 to the broker, which signs the person in with Google (openathena.ai only) or, holding its own
 *  session, sends them straight back. `?next`, `?prompt`, `?account=choose` pass through. Not configured: 404. Without
 *  `prompt=none` this is a person asking to sign in, which ends any suppression of the silent attempt. */
export async function oaStart(cfg: OaLoginCfg | null, request: Request): Promise<Response> {
  if (!cfg) return notFound()
  const res = await oidcStart(cfg)({ request })
  return new URL(request.url).searchParams.get('prompt') === 'none' ? res : resumed(request, res)
}

/** Only a `prompt=none` request gets these, so they answer a silent attempt even when its cookie is gone. */
const NO_INTERACTION = new Set(['login_required', 'interaction_required', 'account_selection_required', 'consent_required'])

/** `/auth/oa/callback`: verify state + nonce, trade the code for the broker's id_token, verify it against the broker's
 *  JWKS, then `gate.signIn(email)` → app session → 302 to `?next` (`signedIn` may decorate that response).
 *
 *  An explicit attempt's failure (the broker's `error=`, or the adapter's own) lands on `/signin?error=oa:<reason>`,
 *  which says why. A silent one's is no error: it lands on the wall, where they were, by way of the adapter
 *  (`<next>?sso=<why>`), or on a quiet `/signin` or `/` when the adapter can't vouch for the state, as with cookies
 *  blocked. `/signin` and `?sso=` never bounce again. */
export async function oaCallback(cfg: OaLoginCfg | null, request: Request, signedIn: (res: Response) => Response = r => r): Promise<Response> {
  if (!cfg) return notFound()
  const refused = new URL(request.url).searchParams.get('error')
  const inFlight = attemptInFlight(request)
  const silent = inFlight || (refused !== null && NO_INTERACTION.has(refused))
  if (refused && !silent) return toSignIn(`error=${encodeURIComponent(`oa:${refused}`)}`)
  const res = await oidcCallback(cfg)({ request })
  if (res.status < 400) return signedIn(res)
  const why = res.headers.get('x-oidc-reason') ?? `HTTP ${res.status}`
  if (refused) return toSignIn(`sso=${encodeURIComponent(refused)}`)
  if (!inFlight) return toSignIn(`error=${encodeURIComponent(`oa:${why}`)}`)
  // Nobody asked to see this, so the log is where it's seen.
  console.log(`silent OA sign-in failed: ${why}`)
  return redirect('/?sso=error')
}

const redirect = (location: string): Response => new Response(null, { status: 302, headers: { location, 'cache-control': 'no-store' } })
const toSignIn = (query: string): Response => redirect(`/signin?${query}`)
