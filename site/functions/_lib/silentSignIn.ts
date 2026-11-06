/**
 * Silent sign-in (specs/oa-login-rp.md phase 2): an unauthenticated page visit, on a deployment with OA's broker
 * configured (`oaLoginConfig`), goes to `/auth/oa?prompt=none&next=<url>` once before the wall. The broker holding a
 * session sends the person straight back signed in; otherwise they land where they were with `?sso=<why>`, and the SPA
 * shows its wall.
 *
 * The wall itself is the client's (`site/src/AuthGate.tsx`, after whoami), so this runs in the middleware, ahead of the
 * shell: the same decision made before any page is sent, with no flash of the wall and no second load. Nothing here
 * reads D1: a session is judged by its signature (`presentsCredentials`), so a signed-in visit pays nothing.
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
import { clearCookie, isSecureRequest, readCookie, sessionCookie } from '@open-athena/auth'
import { type Ctx, type Env, presentsCredentials, SESSION_COOKIE } from './auth.js'
import type { PolicyEnv } from './og/policy.js'
import { oaLoginConfig } from './oidc.js'

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

/** What a document navigation can reach that isn't a walled page: the Functions (an image, a JSON answer, sign-in
 *  itself), and the SPA's ungated pages (`site/src/Root.tsx`: `/signin`, `/privacy`, a card's `.../og`). */
const NOT_WALLED = /^\/(api|data|assets|auth|og|a|v1|slack)(\/|$)|^\/(signin|privacy)\/?$|\/og$/

/** A visit that goes its own way: a share link's exchange (`key`), a preview link (`og`), the wall's request-access
 *  form (`denied`), or one this attempt already answered (`sso`). */
const OWN_WAY = ['key', 'og', 'denied', 'sso']

/** Nothing is walled here: a public deployment. `OG_ANONYMOUS` (`anonymousPolicy`) is not a reason: it sets only what an
 *  unfurler's card shows (`anonymousPage`, `stampPage`); a person's page on a `shape` or `full` deployment is still walled. */
const servesAnonymously = (env: Env & PolicyEnv): boolean => !!env.PUBLIC_READ || env.AUTH_MODE === 'public'

/** The redirect to the broker for this request, or null to serve it as ever. */
export async function silentAttempt(ctx: { request: Request; env: Env & PolicyEnv }): Promise<Response | null> {
  const { request, env } = ctx
  if (!isDocumentNavigation(request)) return null
  const url = new URL(request.url)
  // `//host` is what `next` refuses (the adapter's `safeNext` would send them home): the wall, where they asked to be.
  if (NOT_WALLED.test(url.pathname) || url.pathname.startsWith('//')) return null
  if (OWN_WAY.some(k => url.searchParams.has(k))) return null
  if (readCookie(request, SKIP_COOKIE) || request.headers.has('authorization')) return null
  if (servesAnonymously(env) || !oaLoginConfig(env, request)) return null
  if (await presentsCredentials(ctx as Ctx)) return null
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
export const markedSignedOut = (request: Request, env: Env, res: Response): Response =>
  oaLoginConfig(env, request) ? withCookie(res, skipCookie(request, 'signout', SIGNED_OUT_TTL_S)) : res

/** An explicit `/auth/oa` start's response, the no-attempt state cleared: the person has asked to sign in. */
export const resumed = (request: Request, res: Response): Response =>
  readCookie(request, SKIP_COOKIE) ? withCookie(res, clearCookie({ name: SKIP_COOKIE, secure: isSecureRequest(request) })) : res

/** Any route's response: one minting the session (every sign-in path converges on it) also clears the no-attempt state.
 *  Everything else, and everything on an unconfigured deployment, is `res` itself. */
export function clearedOnSignIn(request: Request, env: Env, res: Response): Response {
  if (!readCookie(request, SKIP_COOKIE)) return res
  const name = `${SESSION_COOKIE}=`
  const minted = res.headers.getSetCookie().some(c => c.startsWith(name) && !c.startsWith(`${name};`))
  return minted && oaLoginConfig(env, request) ? resumed(request, res) : res
}
