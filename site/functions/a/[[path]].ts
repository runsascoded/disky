/**
 * `GET /a/<sha256>.<ext>`: an activity post's sender face (`_lib/publicAvatar.ts`), public so Slack and Discord can
 * fetch it. Only the exact SHA-256 of bytes a post stored resolves; any other path (an email, a grant id or name, a
 * prefix, a bare `/a/`) is a 404, and nothing lists what's stored. Immutable: the name is the content.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { readAvatar } from '../_lib/publicAvatar.js'

interface AvatarCtx { request: Request; env: { DB?: D1Database } }

const notFound = (): Response => new Response('not found\n', { status: 404, headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' } })

export async function onRequest(ctx: AvatarCtx): Promise<Response> {
  const method = ctx.request.method
  if (method !== 'GET' && method !== 'HEAD') return new Response(null, { status: 405, headers: { allow: 'GET, HEAD' } })
  if (!ctx.env.DB) return notFound()
  const img = await readAvatar(ctx.env.DB, new URL(ctx.request.url).pathname)
  if (!img) return notFound()
  return new Response(method === 'HEAD' ? null : img.bytes, {
    headers: {
      'content-type': img.type,
      'cache-control': 'public, max-age=31536000, immutable',
      'x-content-type-options': 'nosniff',
      'content-security-policy': "default-src 'none'",
    },
  })
}
