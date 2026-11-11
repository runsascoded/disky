/**
 * Every request: a person's signed-out visit to the fleet page (`/fleet`, the one walled page) goes to OA's broker once,
 * silently (`@disk-tree/pages-kit/oa`, as `site/`'s); the docs and everything else are public. A response that mints
 * the session clears the no-attempt state.
 */
import { clearedOnSignIn, silentAttempt } from '@disk-tree/pages-kit/oa'
import { type Ctx, identify, oaConfig, SESSION_COOKIE } from './_lib/env.js'

/** Everything but `/fleet` (and `/fleet/`): the docs, the Functions, the assets, `/signin`. */
export const NOT_WALLED = /^(?!\/fleet\/?$)/

export const onRequest = async (ctx: Ctx & { next: () => Promise<Response> }): Promise<Response> => {
  const configured = () => oaConfig(ctx.env, ctx.request) !== null
  const silent = await silentAttempt(ctx.request, {
    notWalled: NOT_WALLED,
    anonymous: () => false,
    configured,
    presents: async () => (await identify(ctx)) !== null,
  })
  if (silent) return silent
  return clearedOnSignIn(ctx.request, SESSION_COOKIE, configured, await ctx.next())
}
