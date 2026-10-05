/**
 * Full-info share links (specs/done/dogi.md): a per-view token that upgrades one
 * exact view's card from anonymous to labelled.
 *
 *   POST   /api/og/mint {url, days?}  → {url, token, exp_day}: the page URL with
 *                                       `og=<token>`. Full viewers (not read-only
 *                                       guest links): a token outlives any session.
 *   GET    /api/og/tokens             → every mint, newest first (admin)
 *   DELETE /api/og/tokens/<token>     → revoke it (admins any; a minter their own)
 */
import { type Ctx, json, requireAdmin, requireStager } from '../../_lib/auth.js'
import { deploymentKey, siteOf, type OgEnv } from '../../_lib/og/serve.js'
import { listTokens, mint, revoke, TOKEN_DAYS_DEFAULT } from '../../_lib/og/tokens.js'

const now = () => Math.floor(Date.now() / 1000)

export const onRequest = async (ctx: Ctx & { env: OgEnv }): Promise<Response> => {
  const { request, env } = ctx
  const url = new URL(request.url)
  const segs = url.pathname.replace(/^\/api\/og\/?/, '').split('/').filter(Boolean)
  if (!env.DB) return json({ error: 'no DB' }, 503)
  const key = deploymentKey(env)
  if (!key) return json({ error: 'cards not configured' }, 503)

  if (segs[0] === 'mint' && segs.length === 1 && request.method === 'POST') {
    const id = await requireStager(ctx)
    if (id instanceof Response) return id
    const body = (await request.json().catch(() => null)) as { url?: unknown; days?: unknown } | null
    let page: URL
    try {
      page = new URL(String(body?.url ?? ''), url.origin)
    } catch {
      return json({ error: 'url required' }, 400)
    }
    if (page.origin !== url.origin) return json({ error: 'url must be on this site' }, 400)
    try {
      const m = await mint(env.DB, siteOf(env).name, page, id.email ?? id.name ?? 'unknown', now(), Number(body?.days) || TOKEN_DAYS_DEFAULT)
      if (!m) return json({ error: 'this page has no card' }, 400)
      return json({ url: m.url, token: m.token, exp_day: m.expDay }, 201)
    } catch (e) {
      if (/no such table/i.test(String((e as Error).message))) return json({ error: 'share cards not set up here (no og_tokens table)' }, 501)
      throw e
    }
  }

  // Revoke: admins any token; a minter their own (as stagers unstage their own items).
  if (segs[0] === 'tokens' && segs.length === 2 && request.method === 'DELETE') {
    const id = await requireStager(ctx)
    if (id instanceof Response) return id
    const who = id.email ?? id.name ?? 'unknown'
    const n = await revoke(env.DB, segs[1], who, now(), id.admin ? undefined : who)
    return n ? json({ revoked: n }) : json({ error: id.admin ? 'no live token' : 'no live token of yours' }, 404)
  }
  if (segs[0] === 'tokens' && segs.length === 1 && request.method === 'GET') {
    const id = await requireAdmin(ctx)
    if (id instanceof Response) return id
    return json({ tokens: await listTokens(env.DB) })
  }
  return json({ error: 'not found' }, 404)
}
