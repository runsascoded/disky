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
export function useKeepInView(id: string, key: string): () => void {
  const want = useRef<number | null>(null)
  const arm = useCallback(() => {
    const el = document.getElementById(id)
    want.current = el ? el.getBoundingClientRect().top : null
  }, [id])
  useEffect(() => {
    const top = want.current
    if (top == null) return
    want.current = null
    const place = () => {
      const el = document.getElementById(id)
      if (el) window.scrollBy({ top: el.getBoundingClientRect().top - top, behavior: 'instant' })
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
  }, [key, id])
  return arm
}
