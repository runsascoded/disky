/**
 * `GET /auth/app-handoff` — the macOS app's sign-in wall opens this in the
 * default browser: sign in there, then hand the session to the app from a tiny
 * page, never the SPA (`_lib/appLink.ts`, specs/app-link.md).
 */
import { appHandoff } from '../_lib/appLink.js'
import { type Ctx } from '../_lib/auth.js'

export const onRequest = (ctx: Ctx): Promise<Response> => appHandoff(ctx)
