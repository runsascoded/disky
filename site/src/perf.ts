import { useEffect } from 'react'

// Time-to-render per plot (specs/render-bench.md): one User Timing vocabulary
// per widget load, so DevTools' Performance panel, the `?perf=1` overlay
// (`dev/PerfOverlay.tsx`), the console (`window.__perf.table()`) and the bench
// runner (`bench/`) all read the same marks:
//
//   <widget>:<key>:request    fetch start
//   <widget>:<key>:response   headers in (Server-Timing phases on `detail`)
//   <widget>:<key>:decoded    body parsed into the widget's model
//   <widget>:<key>:painted    the widget's first commit after `decoded`
//                             (requestAnimationFrame after that commit)
//   <widget>:<key>:settled    the widget's last commit before SETTLE_MS of quiet
//
// and the measures between consecutive marks: `wait`, `decode`, `paint`,
// `settle`, plus `total` (request → settled). `settled` is stamped at the last
// commit's frame, not at the end of the quiet window, so `settle` and `total`
// don't carry the 500 ms the helper waited to be sure.
//
// A fetch site calls `perf.start(widget, key)` and drives `track` / `decoded`;
// the widget calls `usePerfCommit(widget)` once, which stamps `painted` and
// `settled` for every load of that widget from its commits. A widget that
// draws from another's fetch (the children table from the map's subtree) is
// a *twin*: `start(…, ['table'])` opens a second load under the same key
// whose request/response/decoded are the map's and whose painted/settled are
// the table's own. Always on — marks are cheap and buffered, and the numbers
// that matter are production's; only the overlay is `?perf=1`-gated. The
// render spy (`dev/renderSpy.ts`) is a sibling: it counts commits, this
// times them.

export type Widget = 'treemap' | 'table' | 'dtm' | 'dtable' | 'series' | 'age'
export const WIDGETS: readonly Widget[] = ['treemap', 'table', 'dtm', 'dtable', 'series', 'age']
export type Phase = 'wait' | 'decode' | 'paint' | 'settle'
export const PHASES: readonly Phase[] = ['wait', 'decode', 'paint', 'settle']
export const SETTLE_MS = 500

type Stamp = 'request' | 'response' | 'decoded' | 'painted' | 'settled'

export interface ServerTiming {
  /** `name;dur=N` entries, ms (counts ride as `dur` too — see `serverTiming()`). */
  phases: Record<string, number>
  /** `name;desc=…` entries (`cache;desc=hit`). */
  desc: Record<string, string>
}

/** Parse a `Server-Timing` header (`fetch;dur=812, spans;dur=40, cache;desc=hit`). */
export function parseServerTiming(header: string | null | undefined): ServerTiming {
  const phases: Record<string, number> = {}
  const desc: Record<string, string> = {}
  if (!header) return { phases, desc }
  for (const entry of header.split(',')) {
    const [nameRaw, ...params] = entry.split(';')
    const name = nameRaw.trim()
    if (!name) continue
    for (const p of params) {
      const eq = p.indexOf('=')
      const k = (eq < 0 ? p : p.slice(0, eq)).trim().toLowerCase()
      let v = eq < 0 ? '' : p.slice(eq + 1).trim()
      if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1)
      if (k === 'dur') {
        const n = Number(v)
        if (Number.isFinite(n)) phases[name] = n
      } else if (k === 'desc') desc[name] = v
    }
  }
  return { phases, desc }
}

/** One widget load: the five stamps (ms on the `now()` clock) + what the response said. */
export interface Load {
  id: number
  widget: Widget
  key: string
  t: Partial<Record<Stamp, number>> & { request: number }
  server: Record<string, number>
  serverDesc: Record<string, string>
  /** `x-cache` (`hit` / `miss` / `kv`), else `Server-Timing`'s `cache;desc=`, else null. */
  cache: string | null
  status: number | null
  /** The fetch rejected or answered non-OK (or was aborted): no later stamps come. */
  failed: boolean
  /** Decoded to nothing drawable (an empty diff, no age rows): the widget never
   *  mounts, so the load closed at decode — painted and settled stamped there. */
  empty: boolean
}

/** What `entries()` returns: phase spans in ms (null until both ends exist). */
export interface PerfEntry {
  id: number
  widget: Widget
  key: string
  /** `request` stamp, ms since the clock's origin. */
  start: number
  wait: number | null
  decode: number | null
  paint: number | null
  settle: number | null
  total: number | null
  server: Record<string, number>
  cache: string | null
  status: number | null
  /** `settled` stamped. */
  done: boolean
  failed: boolean
  empty: boolean
}

export interface ResponseLike {
  status: number
  headers: { get(name: string): string | null }
}

export interface PerfHandle {
  /** Stamp `response` when `p` resolves (and `fail()` when it rejects — an abort
   *  or a network error); resolves to the same response. */
  track<R extends ResponseLike>(p: Promise<R>): Promise<R>
  response(res: ResponseLike): void
  decoded(): void
  /** Explicit stamps for a widget without `usePerfCommit`; both need the
   *  preceding stamp (painted needs decoded, settled needs painted). */
  painted(): void
  settled(): void
  /** Decoded to nothing the widget will draw: closes the load here (decoded,
   *  painted and settled all at this instant) instead of leaving it open for a
   *  commit that never comes. */
  empty(): void
  fail(): void
}

export interface PerfDeps {
  now(): number
  mark(name: string, opts: { startTime: number; detail?: unknown }): void
  measure(name: string, opts: { start: number; end: number }): void
  raf(fn: () => void): void
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(t: unknown): void
}

export interface Perf {
  start(widget: Widget, key: string, twins?: readonly Widget[]): PerfHandle
  /** A render of `widget` committed: `painted` for its decoded-but-unpainted
   *  loads at the next frame; `settled` for its painted loads once SETTLE_MS
   *  pass with no further commit. */
  commit(widget: Widget): void
  entries(): PerfEntry[]
  /** `console.table` of `entries()`. */
  table(): void
  subscribe(fn: () => void): () => void
  loads: readonly Load[]
  reset(): void
}

const round1 = (x: number) => Math.round(x * 10) / 10

export function createPerf(deps: PerfDeps, settleMs = SETTLE_MS): Perf {
  const loads: Load[] = []
  const subs = new Set<() => void>()
  let nextId = 1
  interface WidgetState { timer: unknown; rafPending: boolean; lastCommit: number; lastPaint: number }
  const widgets = new Map<Widget, WidgetState>()
  const stateOf = (w: Widget): WidgetState => {
    let s = widgets.get(w)
    if (!s) { s = { timer: undefined, rafPending: false, lastCommit: 0, lastPaint: 0 }; widgets.set(w, s) }
    return s
  }
  const notify = () => { for (const s of subs) s() }
  const markName = (l: Load, s: Stamp) => `${l.widget}:${l.key}:${s}`
  const stamp = (l: Load, s: Stamp, at: number, detail?: unknown) => {
    l.t[s] = at
    deps.mark(markName(l, s), detail === undefined ? { startTime: at } : { startTime: at, detail })
  }
  const span = (l: Load, phase: Phase | 'total', from: Stamp, to: Stamp) => {
    const a = l.t[from], b = l.t[to]
    if (a == null || b == null) return
    deps.measure(`${l.widget}:${l.key}:${phase}`, { start: a, end: b })
  }
  const paint = (l: Load, at: number): boolean => {
    if (l.t.decoded == null || l.t.painted != null) return false
    stamp(l, 'painted', at)
    span(l, 'paint', 'decoded', 'painted')
    return true
  }
  const settle = (l: Load, at: number): boolean => {
    if (l.t.painted == null || l.t.settled != null) return false
    stamp(l, 'settled', at)
    span(l, 'settle', 'painted', 'settled')
    span(l, 'total', 'request', 'settled')
    return true
  }
  const handleFor = (ls: Load[]): PerfHandle => {
    const h: PerfHandle = {
      track: p => p.then(r => { h.response(r); return r }, e => { h.fail(); throw e }),
      response(res) {
        const at = deps.now()
        const st = parseServerTiming(res.headers.get('server-timing'))
        const cache = res.headers.get('x-cache') ?? st.desc.cache ?? null
        for (const l of ls) {
          if (l.t.response != null) continue
          l.server = st.phases
          l.serverDesc = st.desc
          l.cache = cache
          l.status = res.status
          stamp(l, 'response', at, { server: st.phases, cache, status: res.status })
          span(l, 'wait', 'request', 'response')
        }
        notify()
      },
      decoded() {
        const at = deps.now()
        for (const l of ls) {
          if (l.t.decoded != null) continue
          stamp(l, 'decoded', at)
          span(l, 'decode', 'response', 'decoded')
        }
        notify()
      },
      painted() { const at = deps.now(); for (const l of ls) paint(l, at); notify() },
      settled() { const at = deps.now(); for (const l of ls) settle(l, at); notify() },
      empty() {
        const at = deps.now()
        for (const l of ls) {
          if (l.t.decoded == null) { stamp(l, 'decoded', at); span(l, 'decode', 'response', 'decoded') }
          paint(l, at)
          settle(l, at)
          l.empty = true
        }
        notify()
      },
      fail() { for (const l of ls) l.failed = true; notify() },
    }
    return h
  }
  const perf: Perf = {
    loads,
    start(widget, key, twins = []) {
      const at = deps.now()
      const ls = [widget, ...twins].map((w): Load => ({
        id: nextId++, widget: w, key, t: { request: at }, server: {}, serverDesc: {}, cache: null, status: null, failed: false, empty: false,
      }))
      for (const l of ls) { loads.push(l); deps.mark(markName(l, 'request'), { startTime: at }) }
      notify()
      return handleFor(ls)
    },
    commit(widget) {
      const ws = stateOf(widget)
      ws.lastCommit = deps.now()
      if (!ws.rafPending) {
        ws.rafPending = true
        deps.raf(() => {
          ws.rafPending = false
          const at = deps.now()
          ws.lastPaint = at
          let changed = false
          for (const l of loads) if (l.widget === widget && paint(l, at)) changed = true
          if (changed) notify()
        })
      }
      deps.clearTimeout(ws.timer)
      ws.timer = deps.setTimeout(() => {
        // A frame never came (a hidden tab pauses rAF): the commit itself is
        // the best paint time we have, for painting and settling alike.
        const at = ws.rafPending ? ws.lastCommit : ws.lastPaint
        let changed = false
        for (const l of loads) {
          if (l.widget !== widget) continue
          if (ws.rafPending && paint(l, at)) changed = true
          if (settle(l, at)) changed = true
        }
        if (changed) notify()
      }, settleMs)
    },
    entries() {
      const ms = (a: number | undefined, b: number | undefined) => (a != null && b != null ? round1(b - a) : null)
      return loads.map(l => ({
        id: l.id,
        widget: l.widget,
        key: l.key,
        start: round1(l.t.request),
        wait: ms(l.t.request, l.t.response),
        decode: ms(l.t.response, l.t.decoded),
        paint: ms(l.t.decoded, l.t.painted),
        settle: ms(l.t.painted, l.t.settled),
        total: ms(l.t.request, l.t.settled),
        server: { ...l.server },
        cache: l.cache,
        status: l.status,
        done: l.t.settled != null,
        failed: l.failed,
        empty: l.empty,
      }))
    },
    table() {
      console.table(perf.entries().map(e => ({
        widget: e.widget, key: e.key, wait: e.wait, decode: e.decode, paint: e.paint, settle: e.settle, total: e.total,
        cache: e.cache, server: Object.entries(e.server).map(([k, v]) => `${k}=${v}`).join(' '),
        state: e.failed ? 'failed' : e.empty ? 'empty' : e.done ? 'settled' : 'loading',
      })))
    },
    subscribe(fn) { subs.add(fn); return () => { subs.delete(fn) } },
    reset() {
      loads.length = 0
      for (const ws of widgets.values()) deps.clearTimeout(ws.timer)
      widgets.clear()
      notify()
    },
  }
  return perf
}

const globalDeps = (): PerfDeps => ({
  now: () => performance.now(),
  mark: (name, opts) => { performance.mark(name, opts) },
  measure: (name, opts) => { performance.measure(name, opts) },
  raf: typeof requestAnimationFrame === 'function' ? fn => { requestAnimationFrame(fn) } : fn => { setTimeout(fn, 0) },
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: t => clearTimeout(t as ReturnType<typeof setTimeout>),
})

/** The page's instance (marks on the real `performance`). */
export const perf: Perf = createPerf(globalDeps())

declare global {
  interface Window { __perf?: Perf }
}
if (typeof window !== 'undefined') window.__perf = perf

/** Report every commit of this component as a commit of `widget`. */
export function usePerfCommit(widget: Widget): void {
  useEffect(() => { perf.commit(widget) })
}
