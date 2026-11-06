/**
 * "Sign in with OA" callback (specs/oa-login-rp.md): verify state + nonce, trade the code for the broker's id_token,
 * verify it against the broker's JWKS, then `gate.signIn(email)` → app session → 302 to `?next`, as `/auth/google`'s
 * callback.
 *
 * An explicit attempt's failure (the broker's `error=`, or the adapter's own) lands on `/signin?error=oa:<reason>`,
 * which says why. A silent one's (`_lib/silentSignIn.ts`) is no error: it lands on the wall, where they were, by way of
 * the adapter (`<next>?sso=<why>`), or on a quiet `/signin` or `/` when the adapter can't vouch for the state, as with
 * cookies blocked. `/signin` and `?sso=` never bounce again.
 */
import { oidcCallback } from '@open-athena/auth/oidc'
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oaLoginConfig } from '../../_lib/oidc.js'
import { attemptInFlight } from '../../_lib/silentSignIn.js'

/** Only a `prompt=none` request gets these, so they answer a silent attempt even when its cookie is gone. */
const NO_INTERACTION = new Set(['login_required', 'interaction_required', 'account_selection_required', 'consent_required'])

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oaLoginConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('Not found\n', { status: 404 })
  const refused = new URL(ctx.request.url).searchParams.get('error')
  const inFlight = attemptInFlight(ctx.request)
  const silent = inFlight || (refused !== null && NO_INTERACTION.has(refused))
  if (refused && !silent) return toSignIn(`error=${encodeURIComponent(`oa:${refused}`)}`)
  const res = await oidcCallback(cfg)({ request: ctx.request })
  if (res.status < 400) return markDevSession(res, ctx.env)
  const why = res.headers.get('x-oidc-reason') ?? `HTTP ${res.status}`
  if (refused) return toSignIn(`sso=${encodeURIComponent(refused)}`)
  if (!inFlight) return toSignIn(`error=${encodeURIComponent(`oa:${why}`)}`)
  // Nobody asked to see this, so the log is where it's seen.
  console.log(`silent OA sign-in failed: ${why}`)
  return redirect('/?sso=error')
}

const redirect = (location: string): Response => new Response(null, { status: 302, headers: { location, 'cache-control': 'no-store' } })
const toSignIn = (query: string): Response => redirect(`/signin?${query}`)
