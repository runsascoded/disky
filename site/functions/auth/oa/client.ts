/**
 * `GET /auth/oa/client` → `{ enabled }`: whether "Sign in with OA" is configured here, so the wall knows to render its
 * button (specs/oa-login-rp.md). Public: it says only that the path exists.
 */
import { type Ctx, json } from '../../_lib/auth.js'
import { oaLoginConfig } from '../../_lib/oidc.js'

export const onRequestGet = (ctx: Ctx): Response =>
  json({ enabled: oaLoginConfig(ctx.env, ctx.request) !== null }, 200, { 'cache-control': 'private, max-age=3600' })
