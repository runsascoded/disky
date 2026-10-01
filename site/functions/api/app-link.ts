/**
 * `POST /api/app-link` — mint the macOS app's single-use sign-in hand-off for
 * the calling session (`_lib/appLink.ts`, specs/app-link.md).
 */
import { mintAppLink } from '../_lib/appLink.js'
import { type Ctx } from '../_lib/auth.js'

export const onRequest = (ctx: Ctx): Promise<Response> => mintAppLink(ctx)
