/** "Sign in with OA" callback (specs/oa-login-rp.md): `@disk-tree/pages-kit/oa`'s, as `site/`'s, over the fleet's gate.
 *  A failure lands on `/signin?error=oa:<reason>` (or, for a silent attempt, quietly back where it started). */
import { oaCallback } from '@disk-tree/pages-kit/oa'
import { type Ctx, oaConfig } from '../../_lib/env.js'

export const onRequest = (ctx: Ctx): Promise<Response> => oaCallback(oaConfig(ctx.env, ctx.request), ctx.request)
