import { useCallback, useEffect, useRef } from 'react'

/** How long a held element is kept in place after the navigation, at most: the next view's sections above it load
 *  and resize the page within this. */
export const HOLD_MS = 4000

/** Hold element `id` where it is on screen across a navigation that would scroll the page. A new path scrolls to the
 *  top (use-prms' section spy: `navScroll`'s `top`), so a drill from the diff took the reader away from the diff they
 *  drilled in. `arm()` before navigating records the element's viewport offset; once `key` (the location) changes,
 *  the page scrolls to put it back there, and keeps it there as the page above it resizes (the map reloading at the
 *  new path) until the reader scrolls, types or clicks, or `HOLD_MS` passes. Declare it after `useSectionSpy`, so
 *  its scroll runs after the spy's. */
export function useKeepInView(id: string, key: string): (which?: string | null) => void {
  const want = useRef<{ id: string; top: number } | null>(null)
  // `which`: another element to hold this time (a crumb holds the section being read: `sectionInView`); null: none
  const arm = useCallback((which?: string | null) => {
    const target = which === undefined ? id : which
    const el = target ? document.getElementById(target) : null
    want.current = target && el ? { id: target, top: el.getBoundingClientRect().top } : null
  }, [id])
  useEffect(() => {
    const held = want.current
    if (held == null) return
    want.current = null
    const place = () => {
      const el = document.getElementById(held.id)
      if (el) window.scrollBy({ top: el.getBoundingClientRect().top - held.top, behavior: 'instant' })
    }
    place()
    const ro = new ResizeObserver(place)
    ro.observe(document.body)
    const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'] as const
    const stop = () => {
      ro.disconnect()
      clearTimeout(timer)
      for (const e of events) window.removeEventListener(e, stop)
    }
    const timer = setTimeout(stop, HOLD_MS)
    for (const e of events) window.addEventListener(e, stop, { passive: true })
    return stop
  }, [key])
  return arm
}

/** The section being read: the first of `ids` whose heading (its top) is on screen below `line` (the sticky bar's bottom
 *  edge) in the upper half of the viewport; else the one the bar cuts across (it fills the screen). Null at the page's
 *  top (nothing scrolled under the bar: a new path's top is where the reader already is). A section whose tail alone is
 *  left under the bar isn't the one being read: on cw, the table's last 18 px above the size chart and the diff. */
export function sectionInView(ids: readonly string[], line: number): string | null {
  if (window.scrollY <= 0) return null
  const rects = ids.flatMap(id => {
    const r = document.getElementById(id)?.getBoundingClientRect()
    return r ? [{ id, top: r.top, bottom: r.bottom }] : []
  })
  const headed = rects.find(r => r.top >= line - 1 && r.top < line + (window.innerHeight - line) / 2)
  return headed?.id ?? rects.find(r => r.top < line && r.bottom > line)?.id ?? null
}
