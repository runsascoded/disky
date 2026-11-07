/** `GET /api/whoami` → `{ email }` for a staff session, `{ email: null, signIn }` otherwise (`signIn`: whether "Sign in
 *  with OA" is configured here, so the page knows to offer it). */
import { type Ctx, identify, json, oaConfig } from '../_lib/env.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> =>
  json({ email: await identify(ctx), signIn: oaConfig(ctx.env, ctx.request) !== null }, 200, { 'cache-control': 'private, no-store' })
