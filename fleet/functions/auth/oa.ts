/** "Sign in with OA" start (specs/oa-login-rp.md): `@disk-tree/pages-kit/oa`'s, as `site/`'s, over the fleet's gate. */
import { oaStart } from '@disk-tree/pages-kit/oa'
import { type Ctx, oaConfig } from '../_lib/env.js'

export const onRequest = (ctx: Ctx): Promise<Response> => oaStart(oaConfig(ctx.env, ctx.request), ctx.request)
