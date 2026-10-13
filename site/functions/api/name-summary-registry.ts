import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { NameQueryError, nameRegistryParams, namesEnabled, type NameSummaryEnv } from '../_lib/nameSummary.js'
import { privateHeaders, staticRegistry } from '../_lib/nameSummaryStatic.js'

export async function onRequest(ctx: Ctx & { env: NameSummaryEnv }): Promise<Response> {
  const identity = await requireViewer({ request: ctx.request, env: { ...ctx.env, PUBLIC_READ: undefined } })
  if (identity instanceof Response) { const headers = new Headers(identity.headers); headers.set('cache-control', privateHeaders['cache-control']); return new Response(identity.body, { status: identity.status, headers }) }
  if (ctx.env.PUBLIC_READ || identity.via === 'public') return json({ error: 'Name summaries require a private viewer-protected deployment.' }, 403, privateHeaders)
  if (!namesEnabled(ctx.env)) return json({ error: 'Name summaries are not enabled.' }, 404, privateHeaders)
  if (ctx.request.method !== 'GET') return json({ error: 'Only GET is supported.' }, 405, { ...privateHeaders, allow: 'GET' })
  if (ctx.env.STORE_KEY !== undefined || ctx.env.STORE_SCOPE !== undefined) return json({ error: 'Name summaries serve the primary frozen index only.' }, 409, privateHeaders)
  try { nameRegistryParams(new URL(ctx.request.url)) } catch (error) {
    if (error instanceof NameQueryError) return json({ error: error.message }, 400, privateHeaders)
    throw error
  }
  return staticRegistry(ctx.env)
}
export const onRequestGet = onRequest
