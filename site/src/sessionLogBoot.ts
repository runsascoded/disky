/**
 * The session log's boot shim (specs/session-log.md) — the only part in the
 * main bundle. It asks `GET /api/session-log` whether this deployment logs
 * this viewer right now; if so it imports the logger chunk (`sessionLog.ts`)
 * and hands it the errors raised meanwhile. Off (the default), it is one
 * request per page load and nothing else. `slog` is the app's hook for events
 * the page can't see on its own (hover-intent prefetches, custom pickers).
 */
import type { Kind, SlogEvent } from '../functions/_lib/sessionLogShape'

type Fields = Record<string, unknown>

let sink: ((k: Kind, f?: Fields) => void) | null = null
let state: 'pending' | 'on' | 'off' = 'pending'
const early: SlogEvent[] = []
const EARLY_MAX = 20

/** Record an app-level event (no-op while off; held briefly while the boot decides). */
export function slog(k: Kind, f: Fields = {}): void {
  if (sink) { sink(k, f); return }
  if (state === 'pending' && early.length < EARLY_MAX) early.push({ t: Date.now(), k, ...f } as SlogEvent)
}

export const BUILD = import.meta.env.VITE_BUILD_SHA ?? ''

/** Decide, then start or stand down. Never throws; never delays the app (call it without awaiting). */
export async function bootSessionLog(): Promise<void> {
  const onError = (e: Event) => {
    const ee = e as ErrorEvent
    slog('error', { msg: String(ee.message || ee.error).slice(0, 300) })
  }
  const onReject = (e: Event) => slog('reject', { msg: String((e as PromiseRejectionEvent).reason).slice(0, 300) })
  addEventListener('error', onError)
  addEventListener('unhandledrejection', onReject)
  let cfg: { enabled?: boolean; until?: number | null } | null = null
  try {
    const r = await fetch('/api/session-log', { credentials: 'same-origin', cache: 'no-store' })
    if (r.ok) cfg = await r.json()
  } catch { /* off */ }
  removeEventListener('error', onError)
  removeEventListener('unhandledrejection', onReject)
  if (!cfg?.enabled || typeof cfg.until !== 'number' || cfg.until <= Date.now()) {
    state = 'off'
    early.length = 0
    return
  }
  try {
    const m = await import('./sessionLog')
    const { log, logger } = m.start({ until: cfg.until, early: early.splice(0), build: BUILD })
    sink = log
    state = 'on'
    const prevStop = logger.onStop
    logger.onStop = () => { prevStop?.(); sink = null; state = 'off' }
  } catch {
    state = 'off'
    early.length = 0
  }
}
