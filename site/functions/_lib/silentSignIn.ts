/**
 * Silent sign-in (specs/oa-login-rp.md phase 2): an unauthenticated page visit, on a deployment with OA's broker
 * configured (`oaLoginConfig`), goes to `/auth/oa?prompt=none&next=<url>` once before the wall. The mechanism (the skip
 * cookie, its two values, what counts as a person's navigation) is `@disk-tree/pages-kit/oa`'s, shared with `fleet/`;
 * this file says what the site walls.
 *
 * The wall itself is the client's (`site/src/AuthGate.tsx`, after whoami), so this runs in the middleware, ahead of the
 * shell: the same decision made before any page is sent, with no flash of the wall and no second load. Nothing here
 * reads D1: a session is judged by its signature (`presentsCredentials`), so a signed-in visit pays nothing.
 */
import * as oa from '@disk-tree/pages-kit/oa'
import { type Ctx, type Env, presentsCredentials, SESSION_COOKIE } from './auth.js'
import type { PolicyEnv } from './og/policy.js'
import { oaLoginConfig } from './oidc.js'

export { ATTEMPT_TTL_S, attemptInFlight, resumed, SIGNED_OUT_TTL_S, SKIP_COOKIE } from '@disk-tree/pages-kit/oa'

/** What a document navigation can reach that isn't a walled page: the Functions (an image, a JSON answer, sign-in
 *  itself), and the SPA's ungated pages (`site/src/Root.tsx`: `/signin`, `/privacy`, a card's `.../og`). */
const NOT_WALLED = /^\/(api|data|assets|auth|og|a|v1|slack)(\/|$)|^\/(signin|privacy)\/?$|\/og$/

/** Nothing is walled here: a public deployment. `OG_ANONYMOUS` (`anonymousPolicy`) is not a reason: it sets only what an
 *  unfurler's card shows (`anonymousPage`, `stampPage`); a person's page on a `shape` or `full` deployment is still walled. */
const servesAnonymously = (env: Env & PolicyEnv): boolean => !!env.PUBLIC_READ || env.AUTH_MODE === 'public'

/** The redirect to the broker for this request, or null to serve it as ever. */
export function silentAttempt(ctx: { request: Request; env: Env & PolicyEnv }): Promise<Response | null> {
  const { request, env } = ctx
  return oa.silentAttempt(request, {
    notWalled: NOT_WALLED,
    anonymous: () => servesAnonymously(env),
    configured: () => !!oaLoginConfig(env, request),
    presents: () => presentsCredentials(ctx as Ctx),
  })
}

/** Logout's response, this browser marked no-attempt until it resumes. Unconfigured: `res` itself. */
export const markedSignedOut = (request: Request, env: Env, res: Response): Response =>
  oa.markedSignedOut(request, !!oaLoginConfig(env, request), res)

/** Any route's response: one minting the session (every sign-in path converges on it) also clears the no-attempt state.
 *  Everything else, and everything on an unconfigured deployment, is `res` itself. */
export const clearedOnSignIn = (request: Request, env: Env, res: Response): Response =>
  oa.clearedOnSignIn(request, SESSION_COOKIE, () => !!oaLoginConfig(env, request), res)
