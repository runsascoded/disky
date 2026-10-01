/**
 * `GET /auth/app-link?token=…` — the app's webview redeems a hand-off link
 * into an email session (`_lib/appLink.ts`, specs/app-link.md).
 */
import { redeemAppLink } from '../_lib/appLink.js'
import { type Ctx } from '../_lib/auth.js'

export const onRequest = (ctx: Ctx): Promise<Response> => redeemAppLink(ctx)
