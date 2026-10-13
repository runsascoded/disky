/**
 * The `/admin/sessions` viewer's model (specs/session-log.md): one line per
 * event, the URL in effect at each moment ("open this view"), and the strip
 * chart's ticks. Pure, so the rendering is pinned by `sessionsModel.test.ts`.
 */
import { isErrorEvent, type StoredEvent } from '../functions/_lib/sessionLogShape'

/** `1h 02m`, `3m 07s`, `12s`, `450ms`. */
export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

/** `+1:02.345` (offset into the session). */
export function fmtOffset(ms: number): string {
  const t = Math.max(0, Math.round(ms))
  const m = Math.floor(t / 60_000), s = Math.floor((t % 60_000) / 1000), r = t % 1000
  return `+${m}:${String(s).padStart(2, '0')}.${String(r).padStart(3, '0')}`
}

const str = (v: unknown): string => (v === undefined || v === null ? '' : String(v))

/** One event as a line of text. */
export function summarize(e: StoredEvent): string {
  switch (e.k) {
    case 'start': return `page load ${str(e.u)}${e.ref ? ` (from ${str(e.ref)})` : ''}`
    case 'nav': return str(e.u)
    case 'click': return [e.n && `[${str(e.n)}]`, str(e.l), e.x !== undefined && (e.x ? '☑' : '☐')].filter(Boolean).join(' ')
    case 'filter': return `${str(e.n)} = “${str(e.q)}”`
    case 'pick': return `${str(e.n)} → ${str(e.v)}`
    case 'prefetch': return `${str(e.w)} ${str(e.p) || '(root)'}${e.d ? ` @ ${str(e.d)}` : ''}`
    case 'api': {
      const b = e.b && typeof e.b === 'object' ? ` {${Object.entries(e.b).map(([k, v]) => `${k}: ${v}`).join(', ')}}` : ''
      return `${str(e.m) || 'GET'} ${str(e.u)}${b} → ${e.s === 0 ? 'failed' : str(e.s)} · ${str(e.ms)}ms${e.c ? ` · ${str(e.c)}` : ''}${e.qe ? ` · ${str(e.qe)}` : ''}${e.err ? ` · ${str(e.err)}` : ''}`
    }
    case 'error': return `${str(e.msg)}${e.src ? ` @ ${str(e.src)}` : ''}`
    case 'reject': return `unhandled rejection: ${str(e.msg)}`
    case 'console': return `console.error: ${str(e.msg)}`
    case 'viewport': return `${str(e.w)}×${str(e.h)} @${str(e.dpr)}x`
    case 'vis': return `tab ${str(e.s)}`
    case 'dropped': return `${str(e.n)} events dropped (cap)`
  }
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
