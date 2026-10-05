/** Per-view preview tokens in D1 (`og_tokens`, cw `0010` / gcs `0034`; specs/done/dogi.md).
 * A token is 10 random base62 chars and the row is the whole truth: it's good
 * iff a row with that token exists for exactly the request's view (kind +
 * canonical params), unexpired and unrevoked. Each mint is a new token, so
 * each can be revoked alone. A deployment without the table mints nothing and
 * honours no token (every card stays anonymous). */
import type { D1Database } from '@cloudflare/workers-types'
import { canRedeem, hashToken } from '@open-athena/auth'
import { d1GrantStore } from '@open-athena/auth/d1'
import { pageView } from './routes.js'
import { B62, canonical, expDay, expired, randomToken } from './sign.js'

export const TOKEN_CHARS = 10

export const TOKEN_DAYS_DEFAULT = 30
export const TOKEN_DAYS_MAX = 90

export interface Mint {
  token: string
  /** The page URL with `og=<token>` added. */
  url: string
  expDay: number
}

/** Mint a token for `pageUrl`'s view and record it. Null when the URL has no
 * card of its own. */
export async function mint(db: D1Database, site: string, pageUrl: URL, by: string, now: number, days = TOKEN_DAYS_DEFAULT, token = randomToken(TOKEN_CHARS)): Promise<Mint | null> {
  const u = new URL(pageUrl.href)
  u.searchParams.delete('og')
  const pv = pageView(u, site)
  if (!pv) return null
  const day = expDay(now, Math.min(Math.max(1, Math.floor(days)), TOKEN_DAYS_MAX))
  await db.prepare('INSERT INTO og_tokens (token, kind, view, page, minted_by, minted_ts, exp_day) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .bind(token, pv.kind, canonical(pv.params), u.pathname + u.search, by, now, day).run()
  u.searchParams.set('og', token)
  return { token, url: u.href, expDay: day }
}

const TOKEN_RE = new RegExp(`^[${B62}]{${TOKEN_CHARS}}$`)

/** The full tier for this page fetch: the `og=` token's row is for exactly
 * this view, unexpired and unrevoked. Its expiry day, or null. A missing
 * table (or any D1 error) = no. */
export async function fullTier(db: D1Database | undefined, kind: string, params: Record<string, string>, token: string | null, now: number): Promise<{ day: number } | null> {
  if (!token || !db || !TOKEN_RE.test(token)) return null
  try {
    const r = await db.prepare('SELECT exp_day FROM og_tokens WHERE token = ? AND kind = ? AND view = ? AND revoked_ts IS NULL ORDER BY exp_day DESC LIMIT 1')
      .bind(token, kind, canonical(params)).first<{ exp_day: number }>()
    return r && !expired(r.exp_day, now) ? { day: r.exp_day } : null
  } catch {
    return null
  }
}

export interface TokenRow {
  id: number
  token: string
  kind: string
  page: string
  minted_by: string
  minted_ts: number
  exp_day: number
  revoked_by: string | null
  revoked_ts: number | null
}

export async function listTokens(db: D1Database, limit = 200): Promise<TokenRow[]> {
  return (await db.prepare('SELECT id, token, kind, page, minted_by, minted_ts, exp_day, revoked_by, revoked_ts FROM og_tokens ORDER BY minted_ts DESC, id DESC LIMIT ?').bind(limit).all<TokenRow>()).results
}

/** Revoke `token` (one mint); returns how many rows changed. With `minter`,
 * only a row that minter minted (a non-admin revoking their own). */
export async function revoke(db: D1Database, token: string, by: string, now: number, minter?: string): Promise<number> {
  const r = minter == null
    ? await db.prepare('UPDATE og_tokens SET revoked_by = ?, revoked_ts = ? WHERE token = ? AND revoked_ts IS NULL').bind(by, now, token).run()
    : await db.prepare('UPDATE og_tokens SET revoked_by = ?, revoked_ts = ? WHERE token = ? AND minted_by = ? AND revoked_ts IS NULL').bind(by, now, token, minter).run()
  return r.meta.changes ?? 0
}

/** A `key=` share link that would let its bearer in right now, by the
 * gate's own rule for a presented token (`canRedeem`: unrevoked, enabled,
 * unexpired; the redeem cap only limits new sessions), carrying one of
 * `scopes`. Read-only: nothing is redeemed or touched. Such a page unfurls
 * with the full card, since the bearer gets access anyway. */
export async function shareKeyGrant(db: D1Database | undefined, key: string | null, scopes: readonly string[], now: number): Promise<string | null> {
  if (!db || !key || key.length < 16 || key.length > 256) return null
  try {
    const g = await d1GrantStore(db as never).byTokenHash(await hashToken(key))
    return g && grantOk(g, scopes, now) ? g.id : null
  } catch {
    return null
  }
}

export const shareKeyLive = async (db: D1Database | undefined, key: string | null, scopes: readonly string[], now: number): Promise<boolean> =>
  (await shareKeyGrant(db, key, scopes, now)) != null

type GrantLike = { revokedAt: number | null; disabledAt: number | null; expiresAt: number | null; scopes: string[] } & Parameters<typeof canRedeem>[0]
const grantOk = (g: GrantLike, scopes: readonly string[], now: number): boolean =>
  canRedeem(g, now) && g.scopes.some((sc: string) => sc === '*' || scopes.includes(sc))

/** The share-link grant `id` is still live (same rule as `shareKeyGrant`):
 * what a `key=` page's full card image re-checks on every fetch, so
 * revoking the link also reverts its cards. */
export async function grantLive(db: D1Database | undefined, id: string, scopes: readonly string[], now: number): Promise<boolean> {
  if (!db || !id) return false
  try {
    const g = await d1GrantStore(db as never).byId(id)
    return !!g && grantOk(g, scopes, now)
  } catch {
    return false
  }
}

/** A server-minted token for a view (`by` like `slack:staged`): reuses a live
 * row of the same minter and view that has at least `minDays` left, else
 * mints one (`days` long). Recorded and revocable like any other: the
 * staged-plan Slack thread's card goes back to anonymous when its row is
 * revoked on /admin. */
export async function serverToken(db: D1Database, kind: string, params: Record<string, string>, page: string, by: string, now: number, minDays: number, days = TOKEN_DAYS_DEFAULT): Promise<{ token: string; day: number }> {
  const view = canonical(params)
  const row = await db.prepare('SELECT token, exp_day FROM og_tokens WHERE kind = ? AND view = ? AND minted_by = ? AND revoked_ts IS NULL AND exp_day >= ? ORDER BY exp_day DESC LIMIT 1')
    .bind(kind, view, by, expDay(now, minDays)).first<{ token: string; exp_day: number }>()
  if (row) return { token: row.token, day: row.exp_day }
  const token = randomToken(TOKEN_CHARS)
  const day = expDay(now, days)
  await db.prepare('INSERT INTO og_tokens (token, kind, view, page, minted_by, minted_ts, exp_day) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .bind(token, kind, view, page, by, now, day).run()
  return { token, day }
}
