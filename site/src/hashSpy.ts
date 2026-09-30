// A page's section `#hash`, both ways — no dependencies beyond React, so it
// lifts into any app (pass it your router's hash, or `window.location.hash`).
//
// - Deep link: `…#runs` (or any element id) scrolls its target under the
//   sticky header and keeps nudging for a while as the page's sections land
//   (`deps` re-arm it as data arrives; the reader's own scroll ends the
//   pursuit).
// - Scroll-spy: the section in view (`ids`, top to bottom) is written back to
//   the hash with replaceState — no history entries, no jumps — so a reload
//   or a copied URL reopens where the reader was.
import { useEffect } from 'react'

export interface HashSpyOptions {
  /** Section element ids, in page order. */
  ids: readonly string[]
  /** The current `#hash` (from the router, so in-app `<Link to="#x">` navigations are seen too). */
  hash: string
  /** Re-arms the deep link as content lands (query data, lazy sections). */
  deps?: readonly unknown[]
  /** Retired anchor ids → the ids that replaced them. */
  legacy?: Record<string, string>
  /** Height (px) of whatever sticks to the top — where an anchored section parks. */
  offset?: () => number
  /** How long a deep link keeps pursuing its target (ms). */
  pursuitMs?: number
}

// The last hash the spy itself wrote: the deep-link effect must ignore it,
// or a router-driven location change would re-scroll to where the reader is.
let spyHash = ''
// True while a deep link is still scrolling into place; the spy holds off
// until then (at scrollY 0 it would clear the hash before the section exists).
let deepLinkPending = false
// Reader-initiated scrolling (not the programmatic kind) ends a deep link's pursuit.
const USER_SCROLL_EVENTS = ['wheel', 'touchmove', 'keydown'] as const
const NUDGE_MS = 500

const clamp01 = (x: number): number => (x < 0 ? 0 : x > 1 ? 1 : x)

/** The section the spy would name for the current scroll position, or '' at
 * the very top. The reference line sits just under the header for most of
 * the page; over the last viewport's worth of scroll — where the remaining
 * sections can no longer reach it, because the page ends — it slides down
 * to the bottom of the viewport, so each of them still gets its turn (the
 * one deepest into the viewport wins). */
export function sectionInView(ids: readonly string[], offset: number): string {
  const h = window.innerHeight
  const maxY = document.documentElement.scrollHeight - h
  const near = offset + Math.min(h / 3, 150)
  // The line stays fixed at `near` (just under the header) for the whole page —
  // so the named section is whatever sits at the top of the viewport. ONLY over
  // a short window at the very bottom — where the page-end stops the last
  // sections from ever reaching `near` — does it slide down to the viewport
  // bottom, giving each a turn. Confining that slide to `descend` px (not a
  // whole viewport) is what keeps a short page, or a scroll well above the
  // bottom, from mis-naming a section two below the one actually in view.
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

export function useHashSpy({ ids, hash, deps = [], legacy = {}, offset = () => 0, pursuitMs = 20_000 }: HashSpyOptions): void {
  useEffect(() => {
    if (!hash || hash === spyHash) return
    const raw = hash.slice(1)
    const id = legacy[raw] ?? raw
    deepLinkPending = true
    let last = NaN
    let lastH = NaN
    let tries = 0
    const stop = () => {
      clearInterval(iv)
      deepLinkPending = false
      for (const ev of USER_SCROLL_EVENTS) window.removeEventListener(ev, stop)
    }
    const iv = setInterval(() => {
      if (++tries > pursuitMs / NUDGE_MS) { stop(); return }
      const el = document.getElementById(id)
      if (!el) return
      const top = el.getBoundingClientRect().top
      const h = document.documentElement.scrollHeight
      // Park only once the section sits at the offset AND the page height has
      // settled — otherwise late-loading content above (treemap, series, diff,
      // age chart) pushes the section down after we stop, stranding the reader
      // above it. While height still changes, keep re-nudging to follow it.
      if (Math.abs(top - offset()) < 4 && top === last && h === lastH) { stop(); return }
      last = top
      lastH = h
      // Instant, not smooth: this is page-load positioning, not a navigation
      // the reader watches — and a smooth animation restarted every nudge
      // (or paused in a background tab) never gets there.
      el.scrollIntoView({ behavior: 'instant', block: 'start' })
    }, NUDGE_MS)
    for (const ev of USER_SCROLL_EVENTS) window.addEventListener(ev, stop, { passive: true })
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, ...deps])
  useEffect(() => {
    let raf = 0
    const onScroll = () => {
      if (raf || deepLinkPending) return
      raf = requestAnimationFrame(() => {
        raf = 0
        const id = sectionInView(ids, offset())
        const cur = id ? `#${id}` : ''
        if (cur === window.location.hash) return
        spyHash = cur
        history.replaceState(history.state, '', window.location.pathname + window.location.search + cur)
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      if (raf) cancelAnimationFrame(raf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.join(',')])
}
