import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { indexedOnly, reject } from '../_lib/indexedOnly.js'
import { NameQueryError, nameSummaryParams, namesEnabled, type NameSummaryEnv } from '../_lib/nameSummary.js'
import { privateHeaders, staticSummary } from '../_lib/nameSummaryStatic.js'
import { nameLiteral } from '../../src/nameModel.js'

export async function onRequest(ctx: Ctx & { env: NameSummaryEnv }): Promise<Response> {
  const identity = await requireViewer({ request: ctx.request, env: { ...ctx.env, PUBLIC_READ: undefined } })
  if (identity instanceof Response) { const headers = new Headers(identity.headers); headers.set('cache-control', privateHeaders['cache-control']); return new Response(identity.body, { status: identity.status, headers }) }
  if (ctx.env.PUBLIC_READ || identity.via === 'public') return json({ error: 'Name summaries require a private viewer-protected deployment.' }, 403, privateHeaders)
  if (!namesEnabled(ctx.env)) return json({ error: 'Name summaries are not enabled.' }, 404, privateHeaders)
  if (ctx.request.method !== 'GET') return json({ error: 'Only GET is supported.' }, 405, { ...privateHeaders, allow: 'GET' })
  if (ctx.env.STORE_KEY !== undefined || ctx.env.STORE_SCOPE !== undefined) return json({ error: 'Name summaries serve the primary frozen index only.' }, 409, privateHeaders)
  // An indexed-only deployment (`_lib/indexedOnly.ts`): one name literal; `/` is refused with its reason code.
  if (indexedOnly(ctx.env as { FILTER_INDEXED_ONLY?: string }) && (new URL(ctx.request.url).searchParams.get('name') ?? '').includes('/')) {
    const r = reject('unsupported-slash')
    return json({ error: r.message, code: r.code }, 400, privateHeaders)
  }
  // The name as written reads as the map's term does (`nameLiteral`): `\^q`, `q\$` and `"^q"` are literals, sent to
  // the index as such; a bare `^q` / `q$` is an anchor, and /names reads literals only (no anchored reader on any
  // deployment), so it is refused as such — never answered as a literal `^`/`$` substring, which would read as zero matches.
  const url = new URL(ctx.request.url)
  let params: URLSearchParams
  try {
    params = nameSummaryParams(url)
    const name = params.get('name')
    const literal = name == null ? null : nameLiteral(name)
    if (literal?.anchored) {
      const r = reject('anchor-not-indexed')
      return json({ error: r.message, code: r.code }, 400, privateHeaders)
    }
    // The literal, validated as the index will read it (the params are clean once validated, so re-serializing is safe).
    if (literal && literal.text !== name) { params.set('name', literal.text); params = nameSummaryParams(new URL(`?${params}`, url)) }
  } catch (error) {
    if (error instanceof NameQueryError) return json({ error: error.message }, 400, privateHeaders)
    throw error
  }
  return staticSummary(ctx.env, params)
}
export const onRequestGet = onRequest
