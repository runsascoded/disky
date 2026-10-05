/** Which card a fetch gets (specs/done/dogi.md): the page's tier when it's
 * stamped, and the full image's credential re-checked on every image fetch.
 * Kept free of the renderer (no wasm import), so tests drive it directly. */
import type { D1Database } from '@cloudflare/workers-types'
import { baseReadScope, baseScope, type Env } from '../auth.js'
import type { Cred } from './cred.js'
import type { OgKind } from './routes.js'
import { expDay, IMAGE_TTL_DAYS, type OgTier } from './sign.js'
import { fullTier, grantLive, shareKeyGrant } from './tokens.js'

type TierEnv = Pick<Env, 'BASE_SCOPE'> & { DB?: D1Database }

const viewerScopes = (env: TierEnv) => [baseScope(env as Env), baseReadScope(env as Env)]

/** Which card a page fetch earns: `full` for an `og=` token minted for
 * exactly this view (live in D1; the image URL never outlives it), or for a
 * live `key=` share link (its bearer gets in anyway); else `anon`. A full
 * card names its credential, which its image re-checks on every fetch. */
export async function pageTier(env: TierEnv, kind: OgKind, params: Record<string, string>, url: URL, t: number): Promise<{ tier: OgTier; day?: number; cred: Cred | null }> {
  const og = url.searchParams.get('og')
  const tok = await fullTier(env.DB, kind, params, og, t)
  if (tok && og) return { tier: 'full', day: Math.min(expDay(t, IMAGE_TTL_DAYS), tok.day), cred: { t: og } }
  const g = await shareKeyGrant(env.DB, url.searchParams.get('key'), viewerScopes(env), t)
  if (g) return { tier: 'full', day: expDay(t, IMAGE_TTL_DAYS), cred: { g } }
  return { tier: 'anon', cred: null }
}

/** A full image's credential, re-checked now: the token's row for exactly
 * this view (unexpired, unrevoked), or the grant still live. */
export async function credLive(env: TierEnv, kind: string, view: Record<string, string>, cred: Cred | null, t: number): Promise<boolean> {
  if (!cred) return false
  return 't' in cred ? (await fullTier(env.DB, kind, view, cred.t, t)) != null : grantLive(env.DB, cred.g, viewerScopes(env), t)
}
