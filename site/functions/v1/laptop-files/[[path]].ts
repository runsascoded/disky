// CF Pages Function: a file's bytes from the scanned machine, through its
// Cloudflare Tunnel (`_lib/laptopFiles.ts`, specs/laptop-file-tunnel.md).
// The primary store only: a secondary store (the NAS) stays metadata-only.
import { type Ctx, requireAdmin } from '../../_lib/auth.js'
import { d1Deps, serveLaptopFile } from '../../_lib/laptopFiles.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const url = new URL(ctx.request.url)
  if (url.searchParams.get('store')) return new Response('not found', { status: 404 })
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  return serveLaptopFile(ctx.request, ctx.env, id, { fetch, ...d1Deps(ctx.request, ctx.env, id, url.searchParams.get('path')) })
}
