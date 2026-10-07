/** `me` on the owner-keyed read APIs: `user=me` (`/api/estate`) and
 * `lens=user:me` (`/api/subtree`, `/api/diff`, `/api/series`) resolve to the
 * caller's canonical owner id — the same rule `POST /api/actions` applies to
 * `owner: '@me'`, and what `GET /api/me` reports. A resolved request is keyed
 * (cache, response echo) by the id, never by the literal `me`: the edge
 * cache is shared across viewers. */
import type { Env, Identity } from './auth.js'
import type { Lens } from './index.js'
import { canonId, loadRegistry } from './identity.js'

export const ME = 'me'

/** The canonical owner id for a sign-in email: its `user_emails` row, else
 * the deployment registry's canonical id for the email's handle — never the
 * raw email (an email owner matches no user). A D1 lineage without the
 * `user_emails` table (cw) has no rows to consult: the registry rule alone. */
export async function ownerIdFor(env: Env, email: string): Promise<string> {
  let row: { user: string } | null = null
  if (env.DB) {
    try {
      row = await env.DB.prepare('SELECT user FROM user_emails WHERE email = ?').bind(email.toLowerCase()).first<{ user: string }>()
    } catch (e) {
      if (!/no such table/i.test(String((e as Error).message))) throw e
    }
  }
  return row?.user ?? canonId(email, await loadRegistry(env))
}

/** A `user=` value with `me` resolved against the caller; any other id is
 * returned as given. Null: `me` from a caller with no email to resolve (a
 * guest link without one, an anonymous public read). */
export async function resolveUser(env: Env, id: Identity | null, user: string): Promise<string | null> {
  if (user !== ME) return user
  return id?.email ? ownerIdFor(env, id.email) : null
}

/** `resolveUser` over a parsed lens: `undefined` passes through (no lens),
 * null = an unresolvable `me`. */
export async function resolveLens(env: Env, id: Identity | null, lens: Lens | undefined): Promise<Lens | undefined | null> {
  if (!lens) return undefined
  const key = await resolveUser(env, id, lens.key)
  return key == null ? null : { key }
}

/** A lens back as its `lens=` value (`''` for none): what cache keys and
 * response bodies carry. */
export const lensParam = (lens: Lens | undefined): string => lens ? `user:${lens.key}` : ''

export const ME_UNRESOLVED = "'me' needs a signed-in identity with an email"
