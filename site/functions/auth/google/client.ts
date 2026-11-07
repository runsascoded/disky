/**
 * `GET /auth/google/client` → `{ clientId }`: the public OAuth client id, so
 * the wall knows whether to render Google's in-page button (`oneTap`). Public
 * by design — the id ships in every page that renders the button; the secret
 * never leaves the Functions.
 */
import { type Ctx, json } from '../../_lib/auth.js'

export const onRequestGet = ({ env }: Ctx): Response =>
  json({ clientId: env.GOOGLE_CLIENT_ID ?? null }, 200, { 'cache-control': 'private, max-age=3600' })
