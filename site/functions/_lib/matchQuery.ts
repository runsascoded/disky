/** A name-filter request's common front half (`/api/filter-cover`, `/api/name-matches`): the store and its path store
 *  (`withPathStore`: the static index's generation and the interval store for the dates it holds), `date` / `d`, `path`
 *  and `q` / `qs` parsed and checked, the viewer gated, the scan resolved and indexed, the static index's hex-run rule
 *  applied and an indexed-only deployment's gate passed. Returns what the route reads next, or the refusal. */
import { type Env, requireViewer } from './auth.js'
import { storeReady, withPathStore } from './index.js'
import { queryParam, QueryError } from './scope.js'
import { hexQuery, indexedGate } from './staticFilter.js'
import { indexedOnly, pathTerms, rejectBody, rejectQuery } from './indexedOnly.js'
import { serverTiming } from './edgeCache.js'
import { withStore } from './stores.js'
import { isScanId } from '../../src/scanSlug.js'
import { indexedScan, noScan, scanArg } from './scanArg.js'
import { traceJoins } from './shared.js'

export type MatchCtx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

export type MatchQuery = {
  ctx: MatchCtx
  st: ReturnType<typeof serverTiming>
  url: URL
  date: string
  path: string
  qRaw: string
  qp: ReturnType<typeof queryParam>
  query: NonNullable<ReturnType<typeof queryParam>['query']>
}

const jsonRes = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const rejected = (body: string) => new Response(body, { status: 400, headers: { 'content-type': 'application/json' } })

/** `route` names the API in its 503s; `negError` (a `{error, code}` body): refuse a filter with exclusions. */
export async function matchQuery(ctx0: MatchCtx, route: string, negError?: { error: string; code: string }): Promise<MatchQuery | Response> {
  const sc = withStore(ctx0)
  if (sc instanceof Response) return sc
  const ctx = { ...sc, env: (await withPathStore(sc)).env }
  const st = serverTiming()
  traceJoins(ctx.env, st.trace)
  if (!storeReady(ctx.env)) return new Response(`${route} API not configured (missing index store creds)`, { status: 503 })
  const url = new URL(ctx.request.url)
  let date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!isScanId(date) && !url.searchParams.has('d')) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const qRaw = url.searchParams.get('q') ?? ''
  let qp: ReturnType<typeof queryParam>
  try {
    qp = queryParam(url.searchParams, ctx.env.QUERY_SYNTAX, indexedOnly(ctx.env))
  } catch (e) {
    const r = e instanceof QueryError && indexedOnly(ctx.env) ? rejectQuery(qRaw, pathTerms(ctx.env)) : null
    if (r) return rejected(rejectBody(r))
    if (e instanceof QueryError) return new Response(`bad query: ${e.message}`, { status: 400 })
    throw e
  }
  let query = qp.query
  if (!query) return jsonRes({ error: 'q= (a filter) is required' }, 400)
  const refused = indexedOnly(ctx.env) ? rejectQuery(qRaw, pathTerms(ctx.env)) : null
  if (refused) return rejected(rejectBody(refused))
  if (negError && query.neg) return jsonRes(negError, 400)

  const gated = await st.time('auth', requireViewer(ctx as never))
  if (gated instanceof Response) return gated
  if (!url.searchParams.has('date')) {
    const scan = await scanArg(ctx.env, url.searchParams)
    if (scan instanceof Response) return scan
    date = scan
  }
  if (!await st.time('pre', indexedScan(ctx.env, date, true))) return noScan('date', date)
  query = (await hexQuery(ctx.env, query)).query!
  if (indexedOnly(ctx.env)) {
    const r = await st.time('indexed', indexedGate(ctx.env, query.ast, path, [date]))
    if (r) return rejected(rejectBody(r))
  }
  return { ctx, st, url, date, path, qRaw, qp, query }
}
