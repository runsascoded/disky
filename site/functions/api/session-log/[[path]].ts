/** Shared session-log routes, with disky's gates, private activity feed and switch audit. */
import type { D1Database } from '@cloudflare/workers-types'
import { sessionLogRoutes, isForever, lastDay } from '@open-athena/auth/session-log'
import { d1SessionLogStore } from '@open-athena/auth/session-log/d1'
import { type Ctx, type Env as AuthEnv, json, requireAdmin, requireViewer } from '../../_lib/auth.js'
import { feedClient, purgeActivity, readKinds } from '../../_lib/activity.js'
import { keptEvents } from '../../_lib/activityShape.js'
import { noteActivity } from '../../_lib/activityHook.js'
import { latency } from '../../_lib/sessionLatency.js'

type Env = AuthEnv & { SESSION_LOG_RETAIN_DAYS?: string }
type C = Ctx & { env: Env; params?: { path?: string | string[] }; waitUntil?: (p: Promise<unknown>) => void }

// Request-local context is keyed by Request, never stored in one mutable isolate-wide "current request".
// The shared route keeps switch/purge state per database and retention configuration.
const contexts = new WeakMap<Request, C>()
const instances = new WeakMap<D1Database, Map<number, ReturnType<typeof sessionLogRoutes>>>()
const context = (request: Request): C => {
  const ctx = contexts.get(request)
  if (!ctx) throw new Error('session-log request context missing')
  return ctx
}
const retainDays = (raw: string | undefined): number => {
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : 30
}

function routes(db: D1Database, days: number) {
  let byDays = instances.get(db)
  if (!byDays) instances.set(db, (byDays = new Map()))
  let route = byDays.get(days)
  if (!route) {
    route = sessionLogRoutes({
      store: d1SessionLogStore(db),
      retainDays: days,
      // Existing call sites emit only built-ins: pick(n,v) and prefetch(w,p,d).
      appEvents: {},
      identify: async req => {
        const id = await requireViewer(context(req))
        if (id instanceof Response) return null
        return {
          email: id.email, via: id.via,
          ...(id.grantId ? { grantId: id.grantId } : {}),
          ...(id.subject?.name ? { subjectName: id.subject.name } : {}),
        }
      },
      // Keep disky's admin-list/break-glass policy, not a package-default scope shortcut.
      requireAdmin: async req => {
        const id = await requireAdmin(context(req))
        return id instanceof Response ? id : { who: id.email ?? id.name ?? 'unknown' }
      },
      filterEvents: async events => keptEvents(events, await readKinds(db)),
      onStored: async ({ batch, who, req }) => {
        const ctx = context(req)
        if (!ctx.waitUntil) return
        const wait = ctx.waitUntil.bind(ctx)
        const id = await requireViewer(ctx)
        if (id instanceof Response) throw new Error('session-log identity changed during storage')
        // A private activity actor can use its existing grant memo; the public session identity above cannot.
        const actor = who.email ?? (who.via === 'grant' && id.name ? `grant:${id.name}` : `sid:${batch.sid}`)
        await feedClient(ctx.env, { actor, via: who.via, origin: new URL(req.url).origin }, batch, wait)
      },
      onSwitch: async ({ prior, row, req }) => {
        await db.prepare("INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES ('session_log_switch', '1', ?, ?, ?, ?, ?)")
          .bind(prior ? 'update' : 'insert', row.who, row.ts, prior ? JSON.stringify(prior) : null, JSON.stringify(row)).run()
        noteActivity(context(req), {
          line: row.until_ms === null ? 'turned session logging off'
            : isForever(row.until_ms) ? 'turned session logging on, no end date'
            : `turned session logging on through ${lastDay(row.until_ms)}`,
        })
      },
      onPurge: cutoff => purgeActivity(db, cutoff),
    })
    byDays.set(days, route)
  }
  return route
}

export const onRequest = async (ctx: C): Promise<Response> => {
  const path = new URL(ctx.request.url).pathname.replace(/^\/api\/session-log\/?/, '')
  if (path === 'latency') {
    if (ctx.request.method !== 'GET') return json({ error: 'method not allowed' }, 405)
    const id = await requireAdmin(ctx)
    if (id instanceof Response) return id
    if (!ctx.env.DB) return json({ error: 'no D1 bound' }, 503)
    const q = new URL(ctx.request.url).searchParams
    const days = Math.min(Math.max(Number(q.get('days')) || 7, 1 / 24), 31)
    return json(await latency(ctx.env.DB, { since: Date.now() - days * 86_400_000, email: q.get('email') || undefined, sid: q.get('sid') || undefined }), 200, { 'cache-control': 'no-store' })
  }
  if (!ctx.env.DB) {
    if (!path && ctx.request.method === 'GET') return json({ enabled: false, until: null, reason: 'no D1 bound', on: false, retainDays: retainDays(ctx.env.SESSION_LOG_RETAIN_DAYS), appEvents: {} }, 200, { 'cache-control': 'no-store' })
    const id = path ? await requireAdmin(ctx) : await requireViewer(ctx)
    return id instanceof Response ? id : json({ error: 'no D1 bound' }, 503)
  }
  contexts.set(ctx.request, ctx)
  return await routes(ctx.env.DB, retainDays(ctx.env.SESSION_LOG_RETAIN_DAYS))(ctx.request, { waitUntil: ctx.waitUntil?.bind(ctx) })
    ?? json({ error: 'not found' }, 404)
}
