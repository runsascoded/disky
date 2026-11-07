/**
 * The session log's wire shape and limits (specs/session-log.md), shared by
 * the client logger (`src/sessionLog.ts`), the endpoint
 * (`functions/api/session-log/[[path]].ts`) and the viewer
 * (`src/SessionsPage.tsx`). Pure: no Workers or DOM types.
 */

/** Event kinds; anything else is a 400. */
export const KINDS = ['start', 'nav', 'click', 'filter', 'pick', 'prefetch', 'api', 'error', 'reject', 'console', 'viewport', 'vis', 'dropped'] as const
export type Kind = typeof KINDS[number]

/** One event: a client timestamp (epoch ms), a kind, and the kind's short fields. */
export type SlogEvent = { t: number; k: Kind } & Record<string, string | number | boolean | null | Record<string, string | number>>

/** One client flush. `sid` is per tab (survives reloads), `pid` per page load, `seq` per flush of that load. */
export interface SlogBatch {
  sid: string
  pid: string
  seq: number
  build: string
  events: SlogEvent[]
}

export const LIMITS = {
  /** Server: request body bytes (`sendBeacon` / `keepalive` budgets are 64 KiB). */
  bodyBytes: 65_536,
  /** Client: a post's body stays under this (several posts otherwise). */
  batchBytes: 60_000,
  /** Server: events per batch. */
  batchEvents: 300,
  /** Client: flush once this many are buffered. */
  flushEvents: 200,
  /** Server: events stored per session; client: per page load. */
  sessionEvents: 20_000,
  /** Server: one event's serialized size. */
  eventBytes: 4096,
  /** The switch's latest dated end: through the UTC day this many days ahead (`FOREVER_MS` is the undated one). */
  maxDays: 31,
  /** The switch's end when turned on with no date. */
  defaultDays: 7,
} as const

/** Client-side truncation, per field family. */
export const TRUNC = { label: 60, filter: 200, url: 300, msg: 300, stack: 1500 } as const

/** Browser reports that aren't faults: `ResizeObserver loop …` is Chrome saying a resize callback deferred a frame. */
const BENIGN_ERRORS = [/^ResizeObserver loop /]

/** Whether an error message is one of `BENIGN_ERRORS` (neither logged by the client nor counted by the feed). */
export const benignError = (msg: unknown): boolean => typeof msg === 'string' && BENIGN_ERRORS.some(r => r.test(msg))

/** A request the page cancelled itself (`err: 'abort'`: its `AbortSignal` fired, e.g. a newer selection superseded it). */
export const isCancelled = (e: { k?: unknown, s?: unknown, err?: unknown }): boolean => e.k === 'api' && e.s === 0 && e.err === 'abort'

/** A request that failed: no response (other than the page's own cancel) or a 5xx. */
export const isFailedApi = (e: { k?: unknown, s?: unknown, err?: unknown }): boolean =>
  e.k === 'api' && typeof e.s === 'number' && (e.s >= 500 || (e.s === 0 && !isCancelled(e)))

/** Kinds (and api statuses) that count as errors in the session summary; a cancelled request is not one. */
export const isErrorEvent = (e: Pick<SlogEvent, 'k'> & { s?: unknown, err?: unknown }): boolean =>
  e.k === 'error' || e.k === 'reject' || e.k === 'console' || isFailedApi(e)

const ID = /^[A-Za-z0-9_-]{6,40}$/

/** The end (exclusive) of the UTC day `days` after `now`'s. */
export const endOfUtcDay = (now: number, days: number): number => {
  const d = new Date(now)
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + days + 1)
}

/** Turning the switch on with no date: through the UTC day `LIMITS.defaultDays` from now. */
export const defaultUntil = (now: number): number => endOfUtcDay(now, LIMITS.defaultDays)
/** The latest an admin may set: through the UTC day `LIMITS.maxDays` from now. */
export const maxUntil = (now: number): number => endOfUtcDay(now, LIMITS.maxDays)

/** The switch's "no end date": stored as this `until_ms` (2100-01-01T00:00Z), not NULL (NULL is off) and not a new
 *  column, so every reader that only compares `until_ms` with its clock (the endpoint, the client logger, the
 *  session monitor) needs no change: it never passes. Any stored value at or past it reads as indefinite (a
 *  hand-written far-future end included). Dated ends stay within `LIMITS.maxDays`, so they never reach it. */
export const FOREVER_MS = Date.UTC(2100, 0, 1)
export const isForever = (until: number | null): boolean => until !== null && until >= FOREVER_MS

/** The UTC date (`YYYY-MM-DD`) an exclusive end instant runs through. */
export const lastDay = (until: number): string => new Date(until - 1).toISOString().slice(0, 10)

/** An admin's `until` → the instant logging ends (epoch ms), or why it's refused. A bare date runs through that
 *  UTC day; an ISO instant ends at it; empty = `defaultUntil`. Past, or past `maxUntil`, is refused (a typo can't
 *  turn it on for a year). */
export function parseUntil(raw: string | null | undefined, now: number): { until: number } | { error: string } {
  const s = raw?.trim()
  if (!s) return { until: defaultUntil(now) }
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  const until = day ? Date.UTC(+day[1], +day[2] - 1, +day[3] + 1) : Date.parse(s)
  if (!Number.isFinite(until)) return { error: `unparseable until: ${s}` }
  if (until <= now) return { error: `until ${s} is in the past` }
  if (until > maxUntil(now)) return { error: `until ${s} is more than ${LIMITS.maxDays} days ahead (latest: ${lastDay(maxUntil(now))})` }
  return { until }
}

/** The stored switch (`session_log_switch.until_ms`; null = off, or no row) → whether it's on now, or why not. */
export function switchState(untilMs: number | null, now: number): { until: number | null; reason: string | null } {
  if (untilMs === null) return { until: null, reason: 'off' }
  if (untilMs <= now) return { until: untilMs, reason: 'expired' }
  return { until: untilMs, reason: null }
}

/** `GET` / `PUT /api/session-log/switch` (admin): the switch now, who last set it (epoch s), and the bounds a new
 *  `until` must sit in. `/admin` renders it. `indefinite`: on with no end date (`until` is then `FOREVER_MS`, or the
 *  far-future value stored). */
export interface SwitchState {
  enabled: boolean
  until: number | null
  indefinite: boolean
  reason: string | null
  who: string | null
  ts: number | null
  defaultUntil: number
  maxUntil: number
}

export const isOn = (u: { until: number | null; reason: string | null }): boolean => u.until !== null && u.reason === null

const isScalar = (v: unknown): boolean => v === null || ['string', 'number', 'boolean'].includes(typeof v)

/** A posted body → the batch, or the status and reason it's refused with. */
export function validateBatch(raw: unknown): { batch: SlogBatch } | { status: 400 | 413; error: string } {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { status: 400, error: 'body must be a JSON object' }
  const b = raw as Record<string, unknown>
  if (typeof b.sid !== 'string' || !ID.test(b.sid)) return { status: 400, error: 'bad sid' }
  if (typeof b.pid !== 'string' || !ID.test(b.pid)) return { status: 400, error: 'bad pid' }
  if (!Number.isInteger(b.seq) || (b.seq as number) < 0 || (b.seq as number) > 1_000_000) return { status: 400, error: 'bad seq' }
  if (typeof b.build !== 'string' || b.build.length > 40) return { status: 400, error: 'bad build' }
  if (!Array.isArray(b.events) || !b.events.length) return { status: 400, error: 'events must be a non-empty array' }
  if (b.events.length > LIMITS.batchEvents) return { status: 413, error: `more than ${LIMITS.batchEvents} events` }
  for (const [i, e] of b.events.entries()) {
    if (!e || typeof e !== 'object' || Array.isArray(e)) return { status: 400, error: `event ${i}: not an object` }
    const ev = e as Record<string, unknown>
    if (typeof ev.t !== 'number' || !Number.isFinite(ev.t)) return { status: 400, error: `event ${i}: bad t` }
    if (!(KINDS as readonly unknown[]).includes(ev.k)) return { status: 400, error: `event ${i}: unknown kind ${String(ev.k).slice(0, 20)}` }
    for (const [key, v] of Object.entries(ev)) {
      // One level of nesting: the api event's request-body summary (`b`).
      const ok = isScalar(v) || (v && typeof v === 'object' && !Array.isArray(v) && Object.values(v).every(x => typeof x === 'string' || typeof x === 'number'))
      if (!ok) return { status: 400, error: `event ${i}: bad field ${key.slice(0, 20)}` }
    }
    if (JSON.stringify(ev).length > LIMITS.eventBytes) return { status: 413, error: `event ${i}: over ${LIMITS.eventBytes} bytes` }
  }
  return { batch: { sid: b.sid, pid: b.pid, seq: b.seq as number, build: b.build, events: b.events as SlogEvent[] } }
}

/** The session summary row (`session_log`), as the admin APIs return it. */
export interface SessionRow {
  sid: string
  email: string | null
  via: string | null
  build: string | null
  ua: string | null
  started_ms: number
  last_ms: number
  n_events: number
  n_errors: number
  n_batches: number
  first_url: string | null
  received_ts: number
}

/** A position in the session list's order (`last_ms` desc, then `sid`): a row's `last_ms` and `sid`. */
export interface Cursor { lastMs: number; sid: string }

/** A cursor on the wire and in the page URL: `<last_ms>.<sid>` (sids never hold a `.`). */
export const fmtCursor = (r: Pick<SessionRow, 'last_ms' | 'sid'>): string => `${r.last_ms}.${r.sid}`
export function parseCursor(s: string): Cursor | null {
  const m = /^(\d{1,16})\.([A-Za-z0-9_-]{6,40})$/.exec(s)
  return m ? { lastMs: Number(m[1]), sid: m[2] } : null
}

/** Sessions per page of `/admin/sessions` (the API's default `limit`; at most `SESSIONS_MAX_LIMIT`). */
export const SESSIONS_PAGE = 50
export const SESSIONS_MAX_LIMIT = 500

/** `GET /api/session-log/sessions`: one page, most recently active first. `total` = rows matching the filters,
 *  `offset` = how many of them precede this page; `prev` / `next` are the cursors (`before=` / `after=`) of the
 *  adjacent pages, null at either end. */
export interface SessionPage {
  sessions: SessionRow[]
  total: number
  offset: number
  prev: string | null
  next: string | null
}

/** A stored event with where it came from (the page load and batch), as the session view gets it. */
export type StoredEvent = SlogEvent & { pid: string; seq: number }

const cut = (s: string, n: number): string => s.length > n ? `${s.slice(0, n - 1)}…` : s

const KIND_KEYS = new Set(['kind', 'op', 'action', 'mode'])

/** A JSON request body as counts and kinds only: per top-level key, an array's length, a short `kind`-like
 *  string, else the value's type. Not JSON (or not an object) → its type and length. */
export function bodySummary(body: unknown): Record<string, string | number> | null {
  if (body == null) return null
  if (typeof body !== 'string') return { type: Object.prototype.toString.call(body).slice(8, -1) }
  let j: unknown
  try { j = JSON.parse(body) } catch { return { type: 'text', len: body.length } }
  if (!j || typeof j !== 'object' || Array.isArray(j)) return Array.isArray(j) ? { type: 'array', len: j.length } : { type: typeof j }
  const out: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(j as Record<string, unknown>).slice(0, 12)) {
    out[cut(k, 30)] = Array.isArray(v) ? v.length : KIND_KEYS.has(k) && typeof v === 'string' ? cut(v, 30) : v === null ? 'null' : typeof v
  }
  return out
}

const str = (v: unknown): string => (v === undefined || v === null ? '' : String(v))

/** One event as a line of text. */
export function summarize(e: SlogEvent): string {
  switch (e.k) {
    case 'start': return `page load ${str(e.u)}${e.ref ? ` (from ${str(e.ref)})` : ''}`
    case 'nav': return str(e.u)
    case 'click': return [e.n && `[${str(e.n)}]`, str(e.l), e.x !== undefined && (e.x ? '☑' : '☐')].filter(Boolean).join(' ')
    case 'filter': return `${str(e.n)} = “${str(e.q)}”`
    case 'pick': return `${str(e.n)} → ${str(e.v)}`
    case 'prefetch': return `${str(e.w)} ${str(e.p) || '(root)'}${e.d ? ` @ ${str(e.d)}` : ''}`
    case 'api': {
      const b = e.b && typeof e.b === 'object' ? ` {${Object.entries(e.b).map(([k, v]) => `${k}: ${v}`).join(', ')}}` : ''
      return `${str(e.m) || 'GET'} ${str(e.u)}${b} → ${isCancelled(e) ? 'cancelled' : e.s === 0 ? 'failed' : str(e.s)} · ${str(e.ms)}ms${e.c ? ` · ${str(e.c)}` : ''}${e.qe ? ` · ${str(e.qe)}` : ''}${e.err && !isCancelled(e) ? ` · ${str(e.err)}` : ''}`
    }
    case 'error': return `${str(e.msg)}${e.src ? ` @ ${str(e.src)}` : ''}`
    case 'reject': return `unhandled rejection: ${str(e.msg)}`
    case 'console': return `console.error: ${str(e.msg)}`
    case 'viewport': return `${str(e.w)}×${str(e.h)} @${str(e.dpr)}x`
    case 'vis': return `tab ${str(e.s)}`
    case 'dropped': return `${str(e.n)} events dropped (cap)`
  }
}
