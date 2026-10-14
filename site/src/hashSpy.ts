// A page's section `#hash` and its scroll position across navigation — no
// dependencies beyond React, so it lifts into any app (pass it your router's
// location and navigation type).
//
// What the reader gets:
// - A navigation to another path (a breadcrumb, a drill) starts at the top
//   with no hash — unless its link names a section, which it then scrolls to.
// - Back / forward restores where that history entry was left.
// - A deep link (`…#tbl`) scrolls its section in under the sticky header once,
//   following it as the page's sections land above it (`deps` re-arm it as
//   data arrives) until it settles or the reader scrolls.
// - The scroll-spy names the section in view in the hash, with replaceState
//   only (no history entries), and only from the reader's own scrolling — a
//   programmatic scroll, a restore, or the browser clamping scrollY when the
//   page shrinks never writes it.
//
// The spy writes the hash behind the router's back (a router navigation per
// scroll frame would re-render the app), so the router's `hash` goes stale as
// the reader scrolls: nothing here trusts it except at the moment it changes,
// and callers must not copy it into a navigation (`drillTo` used to, which is
// how a crumb click resurrected a section the reader had long scrolled past).
import { useEffect, useLayoutEffect, useRef } from 'react'

export interface NavLocation {
  pathname: string
  hash: string
  /** The history entry's key (react-router's `location.key`; `default` for an entry it didn't create). */
  key: string
}
export type NavType = 'POP' | 'PUSH' | 'REPLACE'

export type NavScroll =
  | { kind: 'none' }
  | { kind: 'top' }
  | { kind: 'hash'; id: string }
  | { kind: 'restore'; y: number }

/** What a location change does to the scroll position (pure, for tests).
 * `liveHash` is the hash actually in the URL bar before the change (the
 * spy's last write, which the router never saw); `saved` is the position
 * the reader left `next.key`'s entry at, if any. */
export function navScroll(
  prev: NavLocation | null,
  next: NavLocation,
  navType: NavType,
  liveHash: string,
  saved: number | undefined,
): NavScroll {
  const hashTarget: NavScroll = next.hash ? { kind: 'hash', id: next.hash.slice(1) } : { kind: 'none' }
  // First render: a reload (the entry has a position) or a deep link.
  if (!prev) return saved !== undefined ? { kind: 'restore', y: saved } : hashTarget
  const pathChanged = next.pathname !== prev.pathname
  if (navType === 'POP' && next.key !== prev.key) {
    // Back / forward to another entry: where it was left, else where its URL points.
    if (saved !== undefined) return { kind: 'restore', y: saved }
    return next.hash ? hashTarget : pathChanged ? { kind: 'top' } : { kind: 'none' }
  }
  if (pathChanged) return next.hash ? hashTarget : { kind: 'top' }
  // Same path (a param change, a legacy rewrite, use-prms re-syncing the
  // router to the spy's hash): only an explicitly new section moves the page.
  return next.hash && next.hash !== liveHash ? hashTarget : { kind: 'none' }
}

export interface HashSpyOptions {
  /** Section element ids, in page order. */
  ids: readonly string[]
  /** The router's location (pathname, hash, key). */
  location: NavLocation
  /** How it was reached (react-router's `useNavigationType()`). */
  navType: NavType
  /** Re-arms a pending deep link as content lands (query data, lazy sections). */
  deps?: readonly unknown[]
  /** Retired anchor ids → the ids that replaced them. */
  legacy?: Record<string, string>
  /** Height (px) of whatever sticks to the top — the spy's reference line sits just under it. */
  offset?: () => number
  /** How long a deep link or restore keeps pursuing its target (ms). */
  pursuitMs?: number
}

// The hash in the URL bar as of the spy's last write (or the last navigation).
let liveHash = ''
// Last reader input that scrolls the page; a scroll event within
// `USER_SCROLL_MS` of it (or chained to a recent reader scroll — touch
// momentum outlives the gesture) is the reader's, anything else is ours or
// the layout's.
let lastInput = -Infinity
let lastUserScroll = -Infinity
const USER_SCROLL_MS = 1000
const MOMENTUM_MS = 200
const NUDGE_MS = 250
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])
const POS_KEY = 'hashSpy.pos'

/** Whether the scroll happening now is the reader's own. */
export function isUserScroll(now: number): boolean {
  return now - lastInput < USER_SCROLL_MS || now - lastUserScroll < MOMENTUM_MS
}

/** Forget any pending reader input: a navigation's own scroll must not read as theirs. */
export function resetUserScroll(): void {
  lastInput = -Infinity
  lastUserScroll = -Infinity
}

function editable(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName ?? ''))
}

function onInput(e: Event): void {
  if (e.type === 'keydown') {
    const k = e as KeyboardEvent
    if (!SCROLL_KEYS.has(k.key) || editable(k.target)) return
  } else if (e.type === 'pointerdown') {
    // Only a press on the page's own scrollbar (the root element, or past the content width).
    const p = e as PointerEvent
    if (p.target !== document.documentElement && p.clientX < document.documentElement.clientWidth) return
  }
  lastInput = performance.now()
}
const INPUT_EVENTS = ['wheel', 'touchmove', 'keydown', 'pointerdown'] as const

/** The current history entry's key for saved positions: the router's, or —
 * for the entry the page loaded into (react-router calls it `default`, and so
 * would every fresh tab) — one of our own, stamped into its state. */
function entryKey(routerKey: string): string {
  if (routerKey !== 'default') return routerKey
  const st = (history.state ?? {}) as { hsKey?: string }
  if (st.hsKey) return st.hsKey
  const hsKey = `hs-${Math.random().toString(36).slice(2, 10)}`
  history.replaceState({ ...st, hsKey }, '', window.location.href)
  return hsKey
}

function loadPositions(): Record<string, number> {
  try { return JSON.parse(sessionStorage.getItem(POS_KEY) ?? '{}') as Record<string, number> } catch { return {} }
}
function savePosition(key: string, y: number): void {
  try {
    const all = loadPositions()
    all[key] = Math.round(y)
    sessionStorage.setItem(POS_KEY, JSON.stringify(all))
  } catch { /* storage off: back/forward falls back to the URL's section */ }
}
function savedPosition(key: string): number | undefined {
  return loadPositions()[key]
}

const clamp01 = (x: number): number => (x < 0 ? 0 : x > 1 ? 1 : x)

/** The section the spy would name for the current scroll position, or '' at
 * the very top. The reference line sits just under the header for most of
 * the page; over a short window at the very bottom — where the page-end stops
 * the last sections from ever reaching it — it slides down to the viewport
 * bottom, giving each a turn. Confining that slide to `descend` px (not a
 * whole viewport) keeps a short page, or a scroll well above the bottom, from
 * mis-naming a section two below the one actually in view. */
export function sectionInView(ids: readonly string[], offset: number): string {
  const h = window.innerHeight
  const maxY = document.documentElement.scrollHeight - h
  const near = offset + Math.min(h / 3, 150)
  const descend = Math.min(h - near, maxY, 280)
  const progress = maxY > 0 ? clamp01((window.scrollY - (maxY - descend)) / descend) : 0
  const yRef = near + (h - near) * progress
  let cur = ''
  for (const id of ids) {
    const el = document.getElementById(id)
    if (el && el.getBoundingClientRect().top <= yRef) cur = id
  }
  return window.scrollY < 40 ? '' : cur
}

/** Where a section parks: its top at its `scroll-margin-top` (the header's height plus a gap, in CSS). */
const parkedAt = (el: Element): number => parseFloat(getComputedStyle(el).scrollMarginTop) || 0

export function useHashSpy({ ids, location, navType, deps = [], legacy = {}, offset = () => 0, pursuitMs = 20_000 }: HashSpyOptions): void {
  const prev = useRef<NavLocation | null>(null)
  const key = useRef(location.key)
  // The current navigation's target, while it's still worth pursuing (the
  // reader hasn't scrolled): late content re-arms it via `deps`.
  const target = useRef<NavScroll>({ kind: 'none' })
  const stopPursuit = useRef<() => void>(() => {})

  const pursue = () => {
    stopPursuit.current()
    const t = target.current
    if (t.kind !== 'hash' && t.kind !== 'restore') return
    const id = t.kind === 'hash' ? (legacy[t.id] ?? t.id) : ''
    let last = NaN
    let lastH = NaN
    let tries = 0
    const step = (): boolean => {
      const h = document.documentElement.scrollHeight
      if (t.kind === 'restore') {
        // Restore once the page is tall enough to hold the position, and has stopped growing.
        if (Math.abs(window.scrollY - t.y) < 2 && h === lastH) return true
        lastH = h
        window.scrollTo({ top: t.y, behavior: 'instant' })
        return false
      }
      const el = document.getElementById(id)
      if (!el) return false
      const top = el.getBoundingClientRect().top
      // Park only once the section sits at its margin AND the page height has
      // settled — late-loading content above (treemap, series, diff, age
      // chart) otherwise pushes it down after we stop.
      if (Math.abs(top - parkedAt(el)) < 4 && top === last && h === lastH) return true
      last = top
      lastH = h
      // Instant, not smooth: page-load positioning, not a navigation the
      // reader watches — a smooth animation restarted every nudge never lands.
      el.scrollIntoView({ behavior: 'instant', block: 'start' })
      return false
    }
    const stop = () => {
      clearInterval(iv)
      for (const ev of INPUT_EVENTS) window.removeEventListener(ev, cancel)
      stopPursuit.current = () => {}
    }
    // The reader's own scrolling ends the pursuit for good (no re-arm).
    const cancel = (e: Event) => {
      onInput(e)
      if (performance.now() - lastInput > 50) return
      target.current = { kind: 'none' }
      stop()
    }
    const iv = setInterval(() => {
      if (++tries > pursuitMs / NUDGE_MS || step()) stop()
    }, NUDGE_MS)
    for (const ev of INPUT_EVENTS) window.addEventListener(ev, cancel, { passive: true })
    stopPursuit.current = stop
    step()
  }

  // Each location change: decide, before paint, where the page should be.
  useLayoutEffect(() => {
    const from = prev.current
    const curLive = from ? liveHash : location.hash
    const k = entryKey(location.key)
    const act = navScroll(from, location, navType, curLive, savedPosition(k))
    prev.current = location
    key.current = k
    // A router navigation writes the URL, so its hash is the live one again.
    liveHash = location.hash
    if (act.kind === 'none') return
    resetUserScroll()
    target.current = act
    if (act.kind === 'top') {
      stopPursuit.current()
      window.scrollTo({ top: 0, behavior: 'instant' })
      savePosition(k, 0)
      return
    }
    pursue()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key, location.pathname, location.hash])

  // Late content (the map, meta, the scans list) re-arms a still-pending target.
  useEffect(() => {
    if (target.current.kind === 'hash' || target.current.kind === 'restore') pursue()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => () => stopPursuit.current(), [])

  useEffect(() => {
    history.scrollRestoration = 'manual'
    let raf = 0
    const onScroll = () => {
      const now = performance.now()
      if (!isUserScroll(now)) return
      lastUserScroll = now
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        savePosition(key.current, window.scrollY)
        const id = sectionInView(ids, offset())
        const cur = id ? `#${id}` : ''
        if (cur === window.location.hash) return
        liveHash = cur
        history.replaceState(history.state, '', window.location.pathname + window.location.search + cur)
      })
    }
    for (const ev of INPUT_EVENTS) window.addEventListener(ev, onInput, { passive: true, capture: true })
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      for (const ev of INPUT_EVENTS) window.removeEventListener(ev, onInput, { capture: true })
      window.removeEventListener('scroll', onScroll)
      if (raf) cancelAnimationFrame(raf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.join(',')])
}
