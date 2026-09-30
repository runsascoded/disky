/**
 * Actions ledger (specs/actions-ledger.md): attribution as an append-only WAL.
 *
 *   GET  /api/actions          → { owners: [...] } — the live expanded owner
 *                                rows joined to their raw action's
 *                                provenance; the client folds them
 *                                (most-recent-wins) over the tree.
 *   POST /api/actions          → append one action (or an array of them):
 *                                { pattern, owner, set_owner?, memo?, scan? }.
 *                                `owner: null` clears; `'@me'` resolves to the
 *                                actor's canonical user id. Prefix patterns
 *                                only for now (regex expansion is the next
 *                                arc). Admin scope — guests are read-only.
 *
 * Anyone with the base scope can read; the ledger keeps the full
 * who-did-what trail.
 */
import { type Ctx, json, requireAdmin, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { canonId, loadRegistry } from '../_lib/identity.js'

/** gs://marin-<suffix>/<path>/ — the six marin buckets only, dir prefixes only. */
const PREFIX_RE = /^gs:\/\/marin-[a-z0-9-]+\/(?:[^\s]*\/)?$/

interface ActionBody {
  pattern?: string
  set_owner?: boolean
  owner?: string | null
  memo?: string
  scan?: string
}

const bad = (error: string) => ({ error })

function validate(b: ActionBody): { error: string } | {
  pattern: string
  owner: string | null
  memo: string | null
  scan: string
} {
  const pattern = b.pattern ?? ''
  if (!PREFIX_RE.test(pattern) || pattern.length > 512) {
    return bad('pattern must be gs://marin-<bucket>/<path>/ (trailing slash; regex patterns not accepted yet)')
  }
  // Touching the axis = the key is present (null = clear); `set_owner` may
  // also be passed explicitly.
  if (!(b.set_owner ?? 'owner' in b)) return bad('action must set an owner (null to clear)')
  // '@me' = resolve the actor's canonical user id server-side (claims).
  const owner = b.owner ?? null
  if (owner !== null && (typeof owner !== 'string' || owner.length > 128)) return bad('owner must be a user id')
  const memo = b.memo?.slice(0, 1024) ?? null
  const scan = typeof b.scan === 'string' && /^[\d-]{8,16}(T\d{4})?$/.test(b.scan) ? b.scan : 'unknown'
  return { pattern, owner, memo, scan }
}

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  // The ownership ledger is the primary store's: `store=<other>` is a 404.
  const notHere = primaryOnly(ctx)
  if (notHere) return notHere
  const { request, env } = ctx
  if (!env.DB) return json({ error: 'actions backend not configured (DB)' }, 503)

  if (request.method === 'GET') {
    const gated = await requireViewer(ctx)
    if (gated instanceof Response) return gated
    const owners = await env.DB.prepare(
      'SELECT o.prefix, o.owner, o.ts, a.actor AS who, a.memo, a.id AS action_id ' +
      'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id ' +
      'WHERE o.tombstoned IS NULL ORDER BY o.prefix, o.ts',
    ).all()
    return json({ owners: owners.results })
  }

  if (request.method === 'POST') {
    // Assigning is admin-only: non-admins propose deletions by staging, not by
    // writing the ledger directly (specs/share-link-hardening.md).
    const id = await requireAdmin(ctx)
    if (id instanceof Response) return id
    if (!id.email) return json({ error: 'admin identity has no email' }, 403)
    const raw = (await request.json()) as ActionBody | ActionBody[]
    const items = Array.isArray(raw) ? raw : [raw]
    if (!items.length || items.length > 500) return json({ error: 'expected 1–500 actions' }, 400)
    const parsed = items.map(validate)
    const firstErr = parsed.find(p => 'error' in p)
    if (firstErr && 'error' in firstErr) return json(firstErr, 400)
    const ts = Math.floor(Date.now() / 1000)
    const ok = parsed as Exclude<ReturnType<typeof validate>, { error: string }>[]
    if (ok.some(p => p.owner === '@me')) {
      const row = await env.DB.prepare('SELECT user FROM user_emails WHERE email = ?')
        .bind(id.email.toLowerCase()).first<{ user: string }>()
      // No `user_emails` row → the deployment registry's canonical id for the email's
      // handle, never the raw email (an email owner matches no user).
      const me = row?.user ?? canonId(id.email, await loadRegistry(env))
      for (const p of ok) if (p.owner === '@me') p.owner = me
    }
    const stmts = []
    for (const p of ok) {
      stmts.push(
        env.DB.prepare(
          'INSERT INTO actions (actor, ts, scan, pattern, set_owner, owner, memo) VALUES (?, ?, ?, ?, 1, ?, ?)',
        ).bind(id.email, ts, p.scan, p.pattern, p.owner, p.memo),
      )
      // Prefix patterns expand 1:1. The batch runs sequentially inside one
      // transaction, so "newest actions row" is the INSERT just above.
      stmts.push(
        env.DB.prepare(
          'INSERT INTO owner_prefixes (action_id, prefix, owner, ts) SELECT id, ?, ?, ? FROM actions ORDER BY id DESC LIMIT 1',
        ).bind(p.pattern, p.owner, ts),
      )
    }
    await env.DB.batch(stmts)
    return json({ ok: true, count: parsed.length, ts })
  }

  return json({ error: 'method not allowed' }, 405)
}
