/**
 * Personal agent token — self-service, for driving `/api/actions` (and the
 * rest of `/api/*`) from an agent or a shell (specs/actions-ledger.md § API).
 *
 *   GET    /api/token  → { active, created }        — status; never the token
 *   POST   /api/token  → { token, created }          — mint/rotate; token ONCE
 *   DELETE /api/token  → { revoked }                 — revoke
 *
 * The token is a grant carrying the caller's email and the `gcs` scope only —
 * least privilege, so a leaked token can mark but (even for staff) cannot touch
 * admin/cw routes. It authenticates as a plain `Authorization: Bearer <token>`
 * on every request (the gate hashes and compares; only the hash is stored), so
 * there is no "reveal": the raw value is returned exactly once, at mint. Losing
 * it means rotating (POST again), which revokes the old one first.
 *
 * Only a real SSO identity may manage its own token — a grant minting another
 * grant would be privilege escalation, so grant-authenticated callers are 403.
 */
import { baseScope, type Ctx, gateFor, identify, json } from '../_lib/auth.js'

const TOKEN_NAME = 'CLI / agent token'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { request, env } = ctx
  if (!env.DB) return json({ error: 'token backend not configured (DB)' }, 503)
  const gate = gateFor(env)
  if (!gate) return json({ error: 'auth backend not configured (DB / SESSION_SECRET)' }, 503)

  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  if (!id.email) {
    return json({ error: 'a personal token requires a signed-in email — guest links cannot mint one' }, 403)
  }
  if (id.via === 'grant') {
    return json({ error: 'agent tokens cannot mint tokens — manage yours from a browser SSO session' }, 403)
  }
  const email = id.email.toLowerCase()

  if (request.method === 'GET') {
    const row = await env.DB.prepare('SELECT created FROM agent_tokens WHERE email = ?')
      .bind(email).first<{ created: number }>()
    return json({ active: !!row, created: row?.created ?? null })
  }

  if (request.method === 'POST') {
    // Revoke the prior token before minting, so rotation kills a leaked old
    // token the instant a new one is issued rather than leaving two live.
    const prior = await env.DB.prepare('SELECT grant_id FROM agent_tokens WHERE email = ?')
      .bind(email).first<{ grant_id: string }>()
    if (prior?.grant_id) await gate.revoke(prior.grant_id)
    const { grant, token } = await gate.mint({
      email,
      scopes: [baseScope(env)],   // this deployment's viewer scope (`gcs` | `cw`), nothing more
      name: TOKEN_NAME,
      createdBy: email,
      expiresAt: null,   // non-expiring; ends on rotate or DELETE
      maxRedeems: null,  // used directly as a Bearer credential, never redeemed
    })
    const created = Math.floor(Date.now() / 1000)
    await env.DB.prepare(
      'INSERT INTO agent_tokens (email, grant_id, created) VALUES (?, ?, ?) ' +
      'ON CONFLICT (email) DO UPDATE SET grant_id = ?2, created = ?3',
    ).bind(email, grant.id, created).run()
    // The one and only time the raw token leaves the server.
    return json({ token, created })
  }

  if (request.method === 'DELETE') {
    const row = await env.DB.prepare('SELECT grant_id FROM agent_tokens WHERE email = ?')
      .bind(email).first<{ grant_id: string }>()
    if (row?.grant_id) await gate.revoke(row.grant_id)
    await env.DB.prepare('DELETE FROM agent_tokens WHERE email = ?').bind(email).run()
    return json({ revoked: !!row })
  }

  return json({ error: 'method not allowed' }, 405)
}
