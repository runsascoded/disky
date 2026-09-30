/**
 * Lost & found claims (the pre-ledger `claims` table): POST { prefix } to
 * claim an unattributed prefix as yours; POST { prefix, release: true } to
 * release a claim you hold.
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { primaryOnly } from '../_lib/stores.js'

const PREFIX_RE = /^gs:\/\/marin-[a-z0-9-]+\/(?:[^\s]*\/)?$/

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  // The ownership ledger is the primary store's: `store=<other>` is a 404.
  const notHere = primaryOnly(ctx)
  if (notHere) return notHere
  const { request, env } = ctx
  if (request.method !== 'POST') return json({ error: 'method not allowed' }, 405)
  if (!env.DB) return json({ error: 'claims backend not configured (DB)' }, 503)
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  if (!id.email) {
    return json({ error: 'claiming requires a signed-in email — guest links are read-only; sign in via Google or ask for a personal link' }, 403)
  }
  const who = id.email

  const b = (await request.json()) as { prefix?: string; release?: boolean }
  const prefix = b.prefix ?? ''
  if (!PREFIX_RE.test(prefix) || prefix.length > 1024) {
    return json({ error: 'prefix must be gs://marin-<bucket>/<path>/ (trailing slash)' }, 400)
  }
  const ts = Math.floor(Date.now() / 1000)

  if (b.release) {
    const res = await env.DB.prepare('DELETE FROM claims WHERE prefix = ? AND who = ?').bind(prefix, who).run()
    return json({ ok: true, released: (res.meta?.changes ?? 0) > 0 })
  }

  // First claim wins; re-claiming your own is a no-op refresh of `ts`.
  const existing = await env.DB.prepare('SELECT who FROM claims WHERE prefix = ?').bind(prefix).first<{ who: string }>()
  if (existing && existing.who !== who) {
    return json({ error: `already claimed by ${existing.who}` }, 409)
  }
  await env.DB.prepare(
    'INSERT INTO claims (prefix, who, ts) VALUES (?, ?, ?) ON CONFLICT (prefix) DO UPDATE SET ts = ?3',
  ).bind(prefix, who, ts).run()
  return json({ ok: true, prefix, who, ts })
}
