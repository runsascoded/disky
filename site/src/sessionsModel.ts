/**
 * The `/admin/sessions` viewer's model (specs/session-log.md): one line per
 * event, the URL in effect at each moment ("open this view"), and the strip
 * chart's ticks. Pure, so the rendering is pinned by `sessionsModel.test.ts`.
 */
import { isErrorEvent, type SessionPage, type SessionRow, type StoredEvent, summarize } from '../functions/_lib/sessionLogShape'

export { summarize }
import { type Registry, whoToHandle } from './identityRegistry'

/** `4d 12h 43m`, `1h 02m`, `3m 07s`, `12s`, `450ms`. */
export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, '0')}h`
}

/** An offset or gap: `+2.345s` under a minute (to the ms), else two units: `+3m 07s`, `+1h 02m`, `+4d 12h`. */
export function fmtOffset(ms: number): string {
  const t = Math.max(0, Math.round(ms))
  return `+${t < 60_000 ? `${(t / 1000).toFixed(3)}s` : fmtDuration(t)}`
}

/** What the timeline's first column shows: the offset into the session with the gap since the row above
 *  (`+2d 03h (+5m 10s)`), or how long ago (from `now`). */
export const AT_MODES = ['start', 'ago'] as const
export type AtMode = typeof AT_MODES[number]
export const AT_LABEL: Record<AtMode, string> = { start: 'at (Δ)', ago: 'ago' }

export function fmtAt(mode: AtMode, l: Pick<Line, 't' | 'off'>, prev: Pick<Line, 'off'> | null, now: number): string {
  if (mode === 'start') return prev ? `${fmtOffset(l.off)} (${fmtOffset(l.off - prev.off)})` : fmtOffset(l.off)
  return `${fmtDuration(Math.max(0, now - l.t))} ago`
}

/** A local wall-clock time to the ms (`19:43:09.433`), led by the date (`Oct 13 19:43:09.433`) when its day isn't
 *  `startMs`'s (the session header carries the start's date). */
export function fmtClock(ms: number, startMs: number, timeZone?: string): string {
  const day = (t: number) => new Date(t).toLocaleDateString('en-CA', { timeZone })
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZone })
    .formatToParts(new Date(ms)).map(x => [x.type, x.value]))
  const clock = `${p.hour}:${p.minute}:${p.second}.${String(((Math.round(ms) % 1000) + 1000) % 1000).padStart(3, '0')}`
  if (day(ms) === day(startMs)) return clock
  return `${new Date(ms).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone })} ${clock}`
}

/** The strip chart's lanes, top to bottom. */
export const LANES = ['nav', 'input', 'api', 'error', 'page'] as const
export type Lane = typeof LANES[number]
export const laneOf = (e: StoredEvent): Lane =>
  isErrorEvent(e) ? 'error'
    : e.k === 'start' || e.k === 'nav' ? 'nav'
    : e.k === 'click' || e.k === 'filter' || e.k === 'pick' || e.k === 'prefetch' ? 'input'
    : e.k === 'api' ? 'api'
    : 'page'

export interface Line {
  i: number
  t: number
  /** ms since the session's first event. */
  off: number
  k: StoredEvent['k']
  text: string
  error: boolean
  lane: Lane
  /** The URL in effect then: the page load's latest `start` / `nav` at or before this event. */
  url: string | null
  pid: string
}

/** The session's events as timeline lines (they arrive in time order). */
export function timeline(events: readonly StoredEvent[]): Line[] {
  const t0 = events[0]?.t ?? 0
  const urlBy = new Map<string, string>()
  return events.map((e, i) => {
    if ((e.k === 'start' || e.k === 'nav') && typeof e.u === 'string') urlBy.set(e.pid, e.u)
    return { i, t: e.t, off: e.t - t0, k: e.k, text: summarize(e), error: isErrorEvent(e), lane: laneOf(e), url: urlBy.get(e.pid) ?? null, pid: e.pid }
  })
}

export interface Tick { i: number; x: number; lane: Lane; error: boolean }

/** Ticks across `width` px: x by time (a zero-length session puts all at 0). */
export function ticks(lines: readonly Line[], width: number): Tick[] {
  const span = lines.length ? lines[lines.length - 1].off : 0
  return lines.map(l => ({ i: l.i, x: span ? Math.round((l.off / span) * width * 10) / 10 : 0, lane: l.lane, error: l.error }))
}

/** A browser + OS label from a User-Agent: enough to tell a phone from a laptop. */
export function uaLabel(ua: string | null): string {
  if (!ua) return '—'
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'other'
  const os = /iPhone|iPad/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android' : /Mac OS X/.test(ua) ? 'macOS' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : 'other'
  return `${browser} · ${os}`
}

/** Who a session was: a known user (rendered as a `UserChip`), else the raw email, else a guest link with no
 *  bound address (`via: 'grant'`, `email` null — which link isn't recorded), else an anonymous public viewer.
 *  `guest`: signed in through a share link (`via: 'grant'`), not their own sign-in. */
export type Viewer =
  | { kind: 'user'; id: string; email: string; guest: boolean }
  | { kind: 'email'; email: string; guest: boolean }
  | { kind: 'guest' }
  | { kind: 'anonymous' }

/** A session row's viewer. The email → user rule is `ownerIdFor`'s (`functions/_lib/me.ts`): its `user_emails` row
 *  (`emails`, lowercased keys), else a registry entry for the email's handle. Unlike there, an unknown handle is not
 *  a user (it stays the email: no chip for a made-up id). */
export function viewerOf(row: Pick<SessionRow, 'email' | 'via'>, emails: Record<string, string> | undefined, reg: Registry): Viewer {
  const guest = row.via === 'grant'
  const email = row.email
  if (!email) return guest ? { kind: 'guest' } : { kind: 'anonymous' }
  const id = emails?.[email.toLowerCase()] ?? reg[whoToHandle(email)]?.u
  return id ? { kind: 'user', id, email, guest } : { kind: 'email', email, guest }
}

/** The list page's filters and cursor (its URL's `u`, `since`, `err`, `after` / `before`) → the API's query string. */
export function sessionsQuery(f: { email: string; since: string; errors: boolean; after: string | null; before: string | null }): string {
  const q = new URLSearchParams()
  if (f.email) q.set('email', f.email)
  if (f.since) q.set('since', f.since)
  if (f.errors) q.set('errors', '1')
  if (f.after) q.set('after', f.after)
  else if (f.before) q.set('before', f.before)
  return q.toString()
}

/** `51–100 of 312`; `0 of 0` when nothing matches; `none of 312` for a page past the end. */
export function pagerLabel(p: Pick<SessionPage, 'sessions' | 'total' | 'offset'>): string {
  const total = p.total.toLocaleString('en-US')
  if (!p.sessions.length) return p.total ? `none of ${total}` : '0 of 0'
  return `${(p.offset + 1).toLocaleString('en-US')}–${(p.offset + p.sessions.length).toLocaleString('en-US')} of ${total}`
}
