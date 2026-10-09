/**
 * Actions ledger (specs/actions-ledger.md): attribution as an append-only WAL.
 *
 *   GET  /api/actions?log=1    → { total, rows: [...] } — every owner action,
 *                                newest first, with its status (actionLog.ts).
 *   GET  /api/actions          → { owners: [...] } — the live expanded owner
 *                                rows joined to their raw action's
 *                                provenance; the client folds them
 *                                (most-recent-wins) over the tree.
 *   POST /api/actions          → append one action (or an array of them):
 *                                { pattern, owner, set_owner?, memo?, scan? }.
 *                                `owner: null` clears; `'@me'` resolves to the
 *                                actor's canonical user id. Prefix patterns
 *                                only for now (regex expansion is the next
 *                                arc). The assign scope (every signed-in
 *                                viewer, and their agent token) or admin;
 *                                share links are read-only here.
 *
 * Anyone with the base scope can read; the ledger keeps the full
 * who-did-what trail.
 */
import { type Ctx, json, requireAssigner, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { ownerIdFor } from '../_lib/me.js'
import { actionLog } from '../_lib/actionLog.js'
import { hasLedger } from '../_lib/ledger.js'
import { NO_SHAPE, type PrefixShape, prefixShape } from '../_lib/plans.js'

const reEscape = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
/** `<scheme><bucket>/<path>/` over the scanned buckets (`STORE_SCHEME` /
 * `STORE_BUCKETS`), dir prefixes only. */
const prefixRe = (shape: PrefixShape): RegExp =>
  new RegExp(`^${reEscape(shape.scheme)}(?:${shape.buckets.map(reEscape).join('|')})\\/(?:[^\\s]*\\/)?$`)

interface ActionBody {
  pattern?: string
  set_owner?: boolean
  owner?: string | null
  memo?: string
  scan?: string
}

const bad = (error: string) => ({ error })

function validate(b: ActionBody, shape: PrefixShape): { error: string } | {
  pattern: string
  owner: string | null
  memo: string | null
  scan: string
} {
  const pattern = b.pattern ?? ''
  if (!prefixRe(shape).test(pattern) || pattern.length > 512) {
    return bad(`pattern must be ${shape.scheme}<bucket>/<path>/ over a scanned bucket (trailing slash; regex patterns not accepted yet)`)
  }
  // Touching the axis = the key is present (null = clear); `set_owner` may
  // also be passed explicitly.
  if (!(b.set_owner ?? 'owner' in b)) return bad('action must set an owner (null to clear)')
  // '@me' = resolve the actor's canonical user id server-side (assignments).
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
  // cw's D1 (and any lineage without the ledger tables) has no ownership to read or write.
  if (!(await hasLedger(env))) return json({ error: 'no ownership ledger on this deployment' }, 404)

  if (request.method === 'GET') {
    const gated = await requireViewer(ctx)
    if (gated instanceof Response) return gated
    // `?log=1[&limit=&offset=]`: every owner action newest first, with its
    // status (live / superseded / overridden / retracted) — the audit view.
    const u = new URL(request.url)
    if (u.searchParams.get('log')) {
      return json(await actionLog(env, Number(u.searchParams.get('limit') ?? 50), Number(u.searchParams.get('offset') ?? 0)))
    }
    const owners = await env.DB.prepare(
      'SELECT o.prefix, o.owner, o.ts, a.actor AS who, a.memo, a.id AS action_id ' +
      'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id ' +
      'WHERE o.tombstoned IS NULL ORDER BY o.prefix, o.ts',
    ).all()
    return json({ owners: owners.results })
  }

  if (request.method === 'POST') {
    // Any signed-in viewer may assign any prefix to anyone (the ledger keeps
    // who did it); a share link may not, unless minted with the assign scope.
    const id = await requireAssigner(ctx)
    if (id instanceof Response) return id
    if (!id.email) return json({ error: 'assigning needs an identity with an email' }, 403)
    const raw = (await request.json()) as ActionBody | ActionBody[]
    const items = Array.isArray(raw) ? raw : [raw]
    if (!items.length || items.length > 500) return json({ error: 'expected 1–500 actions' }, 400)
    const shape = prefixShape(env)
    if (!shape) return json({ error: `actions ${NO_SHAPE}` }, 503)
    const parsed = items.map(b => validate(b, shape))
    const firstErr = parsed.find(p => 'error' in p)
    if (firstErr && 'error' in firstErr) return json(firstErr, 400)
    const ts = Math.floor(Date.now() / 1000)
    const ok = parsed as Exclude<ReturnType<typeof validate>, { error: string }>[]
    if (ok.some(p => p.owner === '@me')) {
      const me = await ownerIdFor(env, id.email)
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
