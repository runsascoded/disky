/**
 * The session logger (specs/session-log.md): a lazily loaded chunk, imported
 * by `sessionLogBoot.ts` only while the admin switch (`/admin`) is on.
 * `Logger` buffers and flushes (pure: clock, transport and timers are the
 * caller's); `install` wires it to the page — URL changes,
 * clicks on controls, the path filter, pickers, fetches, errors, console
 * errors, viewport and visibility — and returns its undo.
 */
import { LIMITS, type Kind, type SlogBatch, type SlogEvent, TRUNC } from '../functions/_lib/sessionLogShape'

export const ENDPOINT = '/api/session-log'
export const FLUSH_MS = 10_000
export const FILTER_DEBOUNCE_MS = 800
export const RESIZE_DEBOUNCE_MS = 500

type Fields = Record<string, unknown>
/** Sends one batch body; `beacon` on page hide. Resolves to the HTTP status (null: unknown / failed). */
export type Send = (body: string, beacon: boolean) => Promise<number | null>

export const tr = (s: unknown, n: number): string => {
  const str = typeof s === 'string' ? s : String(s)
  return str.length > n ? `${str.slice(0, n - 1)}…` : str
}

const byteLen = (s: string): number => new TextEncoder().encode(s).length

export interface LoggerOpts {
  sid: string
  pid: string
  build: string
  /** Epoch ms when logging ends. */
  until: number
  send: Send
  now?: () => number
}

export class Logger {
  readonly sid: string
  readonly pid: string
  readonly build: string
  readonly until: number
  private send: Send
  private now: () => number
  private buf: SlogEvent[] = []
  private seq = 0
  private dropped = 0
  private total = 0
  private timer: ReturnType<typeof setInterval> | null = null
  stopped = false
  /** Called once, on stop (`install`'s undo). */
  onStop: (() => void) | null = null
  /** Called before each flush (commit debounced inputs). */
  beforeFlush: (() => void) | null = null

  constructor(o: LoggerOpts) {
    this.sid = o.sid
    this.pid = o.pid
    this.build = o.build
    this.until = o.until
    this.send = o.send
    this.now = o.now ?? Date.now
  }

  /** Start the periodic flush. */
  begin(): void {
    if (!this.timer && !this.stopped) this.timer = setInterval(() => this.flush(false), FLUSH_MS)
  }

  /** Record an event (now). Strings past `TRUNC.stack` are cut; nothing else is copied. */
  log(k: Kind, f: Fields = {}): void {
    this.push({ t: this.now(), k, ...f } as SlogEvent)
  }

  /** Record an event with its own timestamp (the boot shim's early errors, a fetch timed from its start). */
  push(e: SlogEvent): void {
    if (this.stopped) return
    if (this.now() >= this.until) { this.stop(); return }
    if (this.total >= LIMITS.sessionEvents) { this.dropped++; return }
    for (const key in e) {
      const v = e[key]
      if (typeof v === 'string' && v.length > TRUNC.stack) (e as Record<string, unknown>)[key] = tr(v, TRUNC.stack)
    }
    this.total++
    this.buf.push(e)
    if (this.buf.length >= LIMITS.flushEvents) this.flush(false)
  }

  /** Send what's buffered, split so each body stays under `LIMITS.batchBytes` (and `batchEvents`). Never awaits
   *  the sends; a 503 (off / expired server-side) stops the logger. */
  flush(beacon: boolean): void {
    if (this.stopped) return
    this.beforeFlush?.()
    if (this.dropped) { this.buf.unshift({ t: this.now(), k: 'dropped', n: this.dropped } as SlogEvent); this.dropped = 0 }
    if (!this.buf.length) return
    const events = this.buf
    this.buf = []
    const head = byteLen(JSON.stringify({ sid: this.sid, pid: this.pid, seq: 999_999, build: this.build, events: [] }))
    let chunk: SlogEvent[] = []
    let size = head
    const emit = () => {
      if (!chunk.length) return
      const b: SlogBatch = { sid: this.sid, pid: this.pid, seq: this.seq++, build: this.build, events: chunk }
      void this.send(JSON.stringify(b), beacon).then(s => { if (s === 503) this.stop() }, () => {})
      chunk = []
      size = head
    }
    for (const e of events) {
      const n = byteLen(JSON.stringify(e)) + 1
      if (chunk.length && (size + n > LIMITS.batchBytes || chunk.length >= LIMITS.batchEvents)) emit()
      chunk.push(e)
      size += n
    }
    emit()
  }

  /** Stop for good: flush what's buffered (events from before the stop), clear the timer, undo the wiring. */
  stop(): void {
    if (this.stopped) return
    const pending = this.buf.length > 0 || this.dropped > 0
    if (pending) this.flush(false)
    this.stopped = true
    this.buf = []
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    const un = this.onStop
    this.onStop = null
    un?.()
  }

  /** Buffered events (tests). */
  get buffered(): readonly SlogEvent[] { return this.buf }
}

/** URL params whose values never leave the page. */
export const SECRET_PARAMS = ['key', 'token', 'code', 'state', 'nonce', 'access_token', 'id_token', 'sig', 'signature', 'secret', 'password']

/** A URL as the log keeps it: same-origin → path + query (+ hash), secrets masked; cross-origin → origin + path. */
export function redactUrl(u: string, origin: string): string {
  let url: URL
  try { url = new URL(u, origin) } catch { return tr(u.split('?')[0], TRUNC.url) }
  if (url.origin !== origin) return tr(url.origin + url.pathname, TRUNC.url)
  for (const k of [...url.searchParams.keys()]) if (SECRET_PARAMS.includes(k.toLowerCase())) url.searchParams.set(k, '…')
  // `URLSearchParams` re-encodes `…`; keep the mask readable.
  return tr(url.pathname + url.search.replace(/%E2%80%A6/g, '…') + url.hash, TRUNC.url)
}

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
    out[tr(k, 30)] = Array.isArray(v) ? v.length : KIND_KEYS.has(k) && typeof v === 'string' ? tr(v, 30) : v === null ? 'null' : typeof v
  }
  return out
}

/** A control's log name and label: its `data-log` (nearest), and its `aria-label`, else text, else title. */
export function describeControl(el: Element): { n?: string; l?: string } {
  const named = el.closest('[data-log]')
  const n = named?.getAttribute('data-log') ?? undefined
  const raw = el.getAttribute('aria-label') ?? (el.textContent ?? '').replace(/\s+/g, ' ').trim() ?? ''
  const l = raw || el.getAttribute('title') || el.getAttribute('name') || el.tagName.toLowerCase()
  return { ...(n ? { n } : {}), l: tr(l, TRUNC.label) }
}

const CONTROLS = '[data-log],button,a[href],[role=button],[role=menuitem],[role=menuitemradio],[role=option],[role=tab],[role=checkbox],[role=switch],summary,input[type=checkbox],input[type=radio]'

const errMsg = (x: unknown): string => {
  if (x instanceof Error) return `${x.name}: ${x.message}`
  if (typeof x === 'string') return x
  try { return JSON.stringify(x) ?? String(x) } catch { return String(x) }
}

/** The page surface `install` touches; `window` / `document` in the app, fakes in tests. */
export interface Page {
  win: EventTarget & {
    location: { href: string; origin: string }
    history: { pushState(...a: unknown[]): void; replaceState(...a: unknown[]): void }
    innerWidth: number
    innerHeight: number
    devicePixelRatio: number
    fetch: typeof fetch
  }
  doc: EventTarget & { visibilityState: string; referrer: string }
  con: { error: (...a: unknown[]) => void }
  perfNow: () => number
}

/** Wire `logger` to the page; returns the undo (restores every patch and listener). */
export function install(p: Page, logger: Logger): () => void {
  const { win, doc, con } = p
  const origin = win.location.origin
  const offs: (() => void)[] = []
  const on = (t: EventTarget, type: string, fn: (e: Event) => void, capture = false) => {
    t.addEventListener(type, fn, capture)
    offs.push(() => t.removeEventListener(type, fn, capture))
  }
  const safe = <A extends unknown[]>(fn: (...a: A) => void) => (...a: A) => { try { fn(...a) } catch { /* the log never breaks the page */ } }

  // URL state: the boot URL, then every change (router pushes, `?d=` / `f=` replaces, back / forward).
  let lastHref = win.location.href
  logger.log('start', { u: redactUrl(lastHref, origin), ...(doc.referrer ? { ref: redactUrl(doc.referrer, origin) } : {}) })
  const navCheck = safe(() => {
    const h = win.location.href
    if (h === lastHref) return
    lastHref = h
    logger.log('nav', { u: redactUrl(h, origin) })
  })
  const hist = win.history
  const push = hist.pushState, replace = hist.replaceState
  hist.pushState = function (this: unknown, ...a: unknown[]) { push.apply(hist, a); navCheck() }
  hist.replaceState = function (this: unknown, ...a: unknown[]) { replace.apply(hist, a); navCheck() }
  offs.push(() => { hist.pushState = push; hist.replaceState = replace })
  on(win, 'popstate', navCheck)
  on(win, 'hashchange', navCheck)

  // Clicks on controls (capture: a handler that stops propagation still gets logged).
  on(doc, 'click', safe((e: Event) => {
    const t = e.target as Element | null
    const el = t?.closest?.(CONTROLS)
    if (!el) return
    const d = describeControl(el)
    const x = (el as HTMLInputElement).type === 'checkbox' || (el as HTMLInputElement).type === 'radio' ? { x: !!(el as HTMLInputElement).checked } : {}
    logger.log('click', { ...d, ...x })
  }), true)

  // The path filter (only `[data-log-input]` fields), debounced; committed before any flush.
  const pendingInput = new Map<string, { q: string; timer: ReturnType<typeof setTimeout> }>()
  const commit = (n: string) => {
    const p0 = pendingInput.get(n)
    if (!p0) return
    clearTimeout(p0.timer)
    pendingInput.delete(n)
    logger.log('filter', { n, q: tr(p0.q, TRUNC.filter) })
  }
  logger.beforeFlush = () => { for (const n of [...pendingInput.keys()]) commit(n) }
  on(doc, 'input', safe((e: Event) => {
    const t = e.target as (Element & { value?: string }) | null
    const el = t?.closest?.('[data-log-input]')
    if (!el || typeof t?.value !== 'string') return
    const n = el.getAttribute('data-log-input') ?? 'input'
    const prev = pendingInput.get(n)
    if (prev) clearTimeout(prev.timer)
    pendingInput.set(n, { q: t.value, timer: setTimeout(() => commit(n), FILTER_DEBOUNCE_MS) })
  }), true)
  offs.push(() => { for (const v of pendingInput.values()) clearTimeout(v.timer) })

  // Pickers: a <select>'s choice.
  on(doc, 'change', safe((e: Event) => {
    const t = e.target as (Element & { value?: string }) | null
    if (!t || t.tagName !== 'SELECT') return
    const n = t.closest('[data-log]')?.getAttribute('data-log') ?? t.getAttribute('aria-label') ?? t.getAttribute('name') ?? t.id ?? 'select'
    logger.log('pick', { n: tr(n, TRUNC.label), v: tr(t.value ?? '', TRUNC.label) })
  }), true)

  // API requests: route, status, duration, cache / engine headers, the error body's code.
  const origFetch = win.fetch
  const wrapped = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (url.includes(ENDPOINT)) return origFetch.call(win, input, init)
    const t = Date.now(), t0 = p.perfNow()
    const method = (init?.method ?? (typeof input === 'object' && 'method' in input ? input.method : 'GET')).toUpperCase()
    const base: Fields = {
      ...(method !== 'GET' ? { m: method } : {}),
      u: redactUrl(url, origin),
      ...(method !== 'GET' && init?.body != null ? { b: bodySummary(init.body) } : {}),
    }
    let res: Response
    try {
      res = await origFetch.call(win, input, init)
    } catch (e) {
      safe(() => logger.push({ t, k: 'api', ...base, s: 0, ms: Math.round(p.perfNow() - t0), err: tr((e as Error)?.name === 'AbortError' ? 'abort' : errMsg(e), TRUNC.msg) } as SlogEvent))()
      throw e
    }
    safe(() => {
      const ms = Math.round(p.perfNow() - t0)
      const h = res.headers
      const ev = { t, k: 'api', ...base, s: res.status, ms, ...(h.get('x-cache') ? { c: h.get('x-cache') } : {}), ...(h.get('x-query-engine') ? { qe: h.get('x-query-engine') } : {}) } as SlogEvent
      if (res.ok) { logger.push(ev); return }
      res.clone().json().then(
        (j: { error?: unknown }) => logger.push(j && typeof j.error === 'string' ? { ...ev, err: tr(j.error, TRUNC.msg) } : ev),
        () => logger.push(ev),
      )
    })()
    return res
  }) as typeof fetch
  win.fetch = wrapped
  offs.push(() => { if (win.fetch === wrapped) win.fetch = origFetch })

  // Client exceptions and unhandled rejections.
  on(win, 'error', safe((e: Event) => {
    const ee = e as ErrorEvent
    logger.log('error', {
      msg: tr(ee.message || errMsg(ee.error), TRUNC.msg),
      ...(ee.filename ? { src: tr(`${redactUrl(ee.filename, origin)}:${ee.lineno}:${ee.colno}`, TRUNC.url) } : {}),
      ...(ee.error instanceof Error && ee.error.stack ? { st: tr(ee.error.stack, TRUNC.stack) } : {}),
    })
  }))
  on(win, 'unhandledrejection', safe((e: Event) => {
    const r = (e as PromiseRejectionEvent).reason
    logger.log('reject', { msg: tr(errMsg(r), TRUNC.msg), ...(r instanceof Error && r.stack ? { st: tr(r.stack, TRUNC.stack) } : {}) })
  }))

  // Console errors (React's own reports land here).
  const origError = con.error
  let inError = false
  con.error = (...a: unknown[]) => {
    if (!inError) {
      inError = true
      try { logger.log('console', { msg: tr(a.map(errMsg).join(' '), TRUNC.msg) }) } catch { /* never breaks console.error */ }
      inError = false
    }
    origError.apply(con, a)
  }
  offs.push(() => { con.error = origError })

  // Viewport (now, and on resize, debounced) and visibility; a hidden page flushes by beacon.
  const viewport = () => logger.log('viewport', { w: win.innerWidth, h: win.innerHeight, dpr: win.devicePixelRatio })
  viewport()
  let rz: ReturnType<typeof setTimeout> | null = null
  on(win, 'resize', () => { if (rz) clearTimeout(rz); rz = setTimeout(safe(viewport), RESIZE_DEBOUNCE_MS) })
  offs.push(() => { if (rz) clearTimeout(rz) })
  on(doc, 'visibilitychange', safe(() => {
    logger.log('vis', { s: doc.visibilityState })
    if (doc.visibilityState === 'hidden') logger.flush(true)
  }))
  on(win, 'pagehide', safe(() => logger.flush(true)))

  return () => { for (const off of offs.splice(0).reverse()) off() }
}

/** A random id for `sid` / `pid` (`[A-Za-z0-9_-]`). */
export const newId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`

const SID_KEY = 'session-log-sid'

/** This tab's session id: kept in `sessionStorage`, so a reload continues the session. */
export function tabSid(): string {
  try {
    const have = sessionStorage.getItem(SID_KEY)
    if (have && /^[A-Za-z0-9_-]{6,40}$/.test(have)) return have
    const sid = newId()
    sessionStorage.setItem(SID_KEY, sid)
    return sid
  } catch {
    return newId()
  }
}

/** The transport: `sendBeacon` on page hide, else a `keepalive` POST through the page's own (unwrapped) fetch. */
export const transport = (f: typeof fetch, nav: { sendBeacon?: (url: string, data: Blob) => boolean }): Send => (body, beacon) => {
  // A refused beacon (the browser's queue is full) falls back to the keepalive POST.
  if (beacon && nav.sendBeacon?.(ENDPOINT, new Blob([body], { type: 'application/json' }))) return Promise.resolve(null)
  return f(ENDPOINT, { method: 'POST', body, keepalive: true, credentials: 'same-origin', headers: { 'content-type': 'application/json' } })
    .then(r => r.status, () => null)
}

/** Start logging on this page until `until`; `early` = what the boot shim held. Returns the `slog` sink. */
export function start({ until, early, build }: { until: number; early: SlogEvent[]; build: string }): { log: (k: Kind, f?: Fields) => void; logger: Logger } {
  const origFetch = window.fetch.bind(window)
  const logger = new Logger({ sid: tabSid(), pid: newId(), build, until, send: transport(origFetch, navigator) })
  const page: Page = { win: window as unknown as Page['win'], doc: document, con: console, perfNow: () => performance.now() }
  logger.onStop = install(page, logger)
  for (const e of early) logger.push(e)
  logger.begin()
  return { log: (k, f) => logger.log(k, f), logger }
}
