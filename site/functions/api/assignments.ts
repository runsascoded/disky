/**
 * GET /api/assignments?date=<scan>
 *
 * The assigner × assignee matrix: every live owner assignment (the ledger's
 * `owner_prefixes`), folded to bytes at `date` by the same assignments fold
 * `/users` uses, then grouped by `(who assigned it, to whom)`.
 * Feeds the `/assignments` heatmap and the homepage `?by=` lens.
 * Every assigner is a person today (`actions.actor`); inferred-attribution
 * signals (W&B, path shapes) are a separate axis — gcs:specs/assignment-provenance.md
 * Phase 2 — and are not in this matrix yet.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'
import { hasLedger } from '../_lib/ledger.js'
import { ownerTotals } from '../_lib/ownerTotals.js'
import { storeReady } from '../_lib/index.js'

interface Cell { by: string; to: string; bytes: number; objects: number; prefixes: string[] }

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  // The ownership ledger is the primary store's: `store=<other>` is a 404.
  const notHere = primaryOnly(ctx)
  if (notHere) return notHere
  const { env, request } = ctx
  if (!env.DB) return json({ error: 'ledger backend not configured (DB)' }, 503)
  // cw's D1 (and any lineage without the ledger tables) has no ownership to read.
  if (!(await hasLedger(env))) return json({ error: 'no ownership ledger on this deployment' }, 404)
  if (!storeReady(env)) return json({ error: 'index reader not configured' }, 503)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const date = new URL(request.url).searchParams.get('date') ?? ''
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'date=YYYY-MM-DD required' }, 400)
  try {
    // Assigner is `actions.actor` (an email); resolve it to a canonical user
    // id so self-assignments land on the diagonal and the UI renders one chip
    // vocabulary. Unmapped emails stay as-is.
    const emap = new Map<string, string>()
    const ue = await env.DB.prepare('SELECT email, user FROM user_emails').all<{ email: string; user: string }>()
    for (const r of ue.results) emap.set(r.email.toLowerCase(), r.user)
    const t = await ownerTotals(env, date)
    const cells = new Map<string, Cell>()
    for (const c of t.assignments) {
      // Live assignments with an assignee only (a repainted assignment's bytes belong to
      // the newer covering assignment).
      if (c.repainted_by || !c.owner) continue
      const by = c.who ? (emap.get(c.who.toLowerCase()) ?? c.who) : 'unknown'
      const to = c.owner
      const key = `${by}\u0000${to}`
      const cell = cells.get(key) ?? { by, to, bytes: 0, objects: 0, prefixes: [] }
      cell.bytes += c.bytes
      cell.objects += c.objects
      cell.prefixes.push(c.prefix)
      cells.set(key, cell)
    }
    const out = [...cells.values()].sort((a, b) => b.bytes - a.bytes)
    return json({ scan: t.scan, head: t.head, cells: out }, 200, { 'cache-control': 'private, no-store' })
  } catch (e) {
    return json({ error: (e as Error).message }, 503)
  }
}
