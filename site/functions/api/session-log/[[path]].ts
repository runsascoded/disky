// /api/session-log — optional, time-boxed user-action logging (specs/session-log.md):
//
//   GET  /api/session-log                 { enabled, until, reason }   anyone (enabled only for a viewer)
//   POST /api/session-log                 a client batch → 204          viewer, while on
//   GET  /api/session-log/switch          SwitchState                   admin
//   PUT  /api/session-log/switch          { enabled, until? } → SwitchState   admin
//   GET  /api/session-log/sessions        { sessions: SessionRow[] }    admin (?email= &since= &errors=1 &limit=)
//   GET  /api/session-log/sessions/:sid   { session, events }           admin
//
// Off unless an admin turns the switch on at `/admin` (`session_log_switch`,
// D1); on, it ends by itself at its `until` (default 7 days, at most 31). The
// switch is read at most once per `SWITCH_TTL_MS` per isolate, so a change
// reaches every tab within a minute. The identity is the request's (never the
// client's say). The retention purge (`SESSION_LOG_RETAIN_DAYS`, default 30)
// rides the config GET and each page load's first batch, at most hourly per
// isolate, whether logging is on or off.
import { type Ctx, type Env as AuthEnv, json, requireAdmin, requireViewer } from '../../_lib/auth.js'
import { cachedSwitch, ingest, listSessions, maybePurge, sessionDetail, type SwitchCache, writeSwitch } from '../../_lib/sessionLog.js'
import { defaultUntil, isOn, LIMITS, maxUntil, parseUntil, type SwitchState, switchState, validateBatch } from '../../_lib/sessionLogShape.js'

type Env = AuthEnv & { SESSION_LOG_RETAIN_DAYS?: string }
type C = Ctx & { env: Env; params?: { path?: string | string[] }; waitUntil?: (p: Promise<unknown>) => void }

/** Per isolate: when the purge last ran. */
export const purgeState = { last: 0 }
/** Per isolate (and per D1 binding — one in production): the last read of the switch. */
const switchCaches = new WeakMap<object, SwitchCache>()
const cacheFor = (db: object): SwitchCache => {
  let c = switchCaches.get(db)
  if (!c) switchCaches.set(db, c = { at: null, row: null })
  return c
}

const NO_STORE = { 'cache-control': 'no-store' }
const NOT_SET_UP = 'session log is not set up on this deployment (no session_log table)'
const notSetUp = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

function purgeLater(ctx: C, now: number): void {
  const db = ctx.env.DB
  if (!db) return
  const p = maybePurge(db, ctx.env.SESSION_LOG_RETAIN_DAYS, now, purgeState).catch(e => console.log(`session-log purge failed: ${(e as Error).message}`))
  if (ctx.waitUntil) ctx.waitUntil(p)
}

/** Whether logging is on now (cached switch, exact expiry), or why not. */
async function current(ctx: C, now: number): Promise<{ until: number | null; reason: string | null }> {
  const db = ctx.env.DB
  if (!db) return { until: null, reason: 'no D1 bound' }
  const row = await cachedSwitch(db, now, cacheFor(db))
  if (row === 'missing') return { until: null, reason: 'not set up' }
  return switchState(row?.until_ms ?? null, now)
}

async function config(ctx: C): Promise<Response> {
  const now = Date.now()
  purgeLater(ctx, now)
  const u = await current(ctx, now)
  if (!isOn(u)) return json({ enabled: false, until: u.until, reason: u.reason }, 200, NO_STORE)
  const id = await requireViewer(ctx)
  if (id instanceof Response) return json({ enabled: false, until: u.until, reason: 'no viewer identity' }, 200, NO_STORE)
  return json({ enabled: true, until: u.until, reason: null }, 200, NO_STORE)
}

async function post(ctx: C): Promise<Response> {
  const now = Date.now()
  if (!isOn(await current(ctx, now))) return json({ error: 'session log is off' }, 503)
  const id = await requireViewer(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB!
  if (Number(ctx.request.headers.get('content-length') ?? 0) > LIMITS.bodyBytes) return json({ error: `body over ${LIMITS.bodyBytes} bytes` }, 413)
  const text = await ctx.request.text()
  if (new TextEncoder().encode(text).length > LIMITS.bodyBytes) return json({ error: `body over ${LIMITS.bodyBytes} bytes` }, 413)
  let raw: unknown
  try { raw = JSON.parse(text) } catch { return json({ error: 'body is not JSON' }, 400) }
  const v = validateBatch(raw)
  if ('error' in v) return json({ error: v.error }, v.status)
  let r
  try {
    r = await ingest(db, { email: id.email, via: id.via }, ctx.request.headers.get('user-agent'), v.batch, Math.floor(now / 1000))
  } catch (e) {
    if (notSetUp(e)) return json({ error: NOT_SET_UP }, 503)
    throw e
  }
  if (v.batch.seq === 0) purgeLater(ctx, now)
  if (r === 'capped') return json({ error: `session over ${LIMITS.sessionEvents} events` }, 429)
  return new Response(null, { status: 204 })
}

/** `GET` / `PUT /api/session-log/switch` (admin). `PUT {enabled: true, until?}` turns it on through `until` (a UTC
 *  date or an ISO instant; empty = 7 days); `{enabled: false}` turns it off. Each change lands in `admin_edits`. */
async function switchRoute(ctx: C): Promise<Response> {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const now = Date.now()
  const bounds = { defaultUntil: defaultUntil(now), maxUntil: maxUntil(now) }
  if (ctx.request.method === 'PUT') {
    let body: { enabled?: unknown; until?: unknown }
    try { body = await ctx.request.json() } catch { return json({ error: 'body is not JSON' }, 400) }
    if (typeof body?.enabled !== 'boolean') return json({ error: 'enabled must be true or false' }, 400)
    if (body.until !== undefined && body.until !== null && typeof body.until !== 'string') return json({ error: 'until must be a date string' }, 400)
    let untilMs: number | null = null
    if (body.enabled) {
      const u = parseUntil(body.until as string | null | undefined, now)
      if ('error' in u) return json({ error: u.error }, 400)
      untilMs = u.until
    }
    try {
      await writeSwitch(db, untilMs, id.email ?? id.name ?? 'unknown', Math.floor(now / 1000))
    } catch (e) {
      if (notSetUp(e)) return json({ error: NOT_SET_UP }, 503)
      throw e
    }
  }
  // Admin reads bypass the cache (and refresh it): the switch as stored, right now — so this isolate's tabs see a
  // change at once, the others within `SWITCH_TTL_MS`.
  const cache = cacheFor(db)
  cache.at = null
  const row = await cachedSwitch(db, now, cache)
  if (row === 'missing') return json({ enabled: false, until: null, reason: 'not set up', who: null, ts: null, ...bounds } satisfies SwitchState, 200, NO_STORE)
  const u = switchState(row?.until_ms ?? null, now)
  return json({ enabled: isOn(u), until: u.until, reason: u.reason, who: row?.who ?? null, ts: row?.ts ?? null, ...bounds } satisfies SwitchState, 200, NO_STORE)
}

async function admin(ctx: C, rest: string): Promise<Response> {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const q = new URL(ctx.request.url).searchParams
  try {
    if (rest === 'sessions') {
      const since = q.get('since')
      const sinceMs = since ? Date.parse(since) : NaN
      const limit = Number(q.get('limit'))
      const sessions = await listSessions(db, {
        email: q.get('email') || undefined,
        since: Number.isFinite(sinceMs) ? sinceMs : undefined,
        errors: q.get('errors') === '1',
        limit: Number.isFinite(limit) && limit > 0 ? limit : undefined,
      })
      return json({ sessions }, 200, NO_STORE)
    }
    const sid = rest.slice('sessions/'.length)
    const d = await sessionDetail(db, sid)
    return d ? json(d, 200, NO_STORE) : json({ error: `no session ${sid}` }, 404)
  } catch (e) {
    if (notSetUp(e)) return json({ error: NOT_SET_UP }, 404)
    throw e
  }
}

export const onRequest = async (ctx: C): Promise<Response> => {
  const p = ctx.params?.path
  const rest = decodeURIComponent((Array.isArray(p) ? p.join('/') : p) ?? '')
  const m = ctx.request.method
  if (rest === '') {
    if (m === 'GET') return config(ctx)
    if (m === 'POST') return post(ctx)
    return json({ error: 'method not allowed' }, 405)
  }
  if (rest === 'switch') {
    return m === 'GET' || m === 'PUT' ? switchRoute(ctx) : json({ error: 'method not allowed' }, 405)
  }
  if (rest === 'sessions' || /^sessions\/[A-Za-z0-9_-]+$/.test(rest)) {
    return m === 'GET' ? admin(ctx, rest) : json({ error: 'method not allowed' }, 405)
  }
  return json({ error: 'not found' }, 404)
}
