import { useEffect, useState } from 'react'
import type { RefObject } from 'react'

/** Whether the element containing `ref`'s node is narrower than `max` CSS px
 *  (phone width) — measured, not a media query, so a table inside a narrow
 *  pane behaves the same as one on a narrow screen, and a test can constrain
 *  the container instead of the window. Measures synchronously before
 *  observing (a hidden tab never gets the first ResizeObserver callback). */
export function useNarrow(ref: RefObject<HTMLElement | null> | undefined, max = 600): boolean {
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    const el = ref?.current?.parentElement
    if (!el) return
    const check = () => setNarrow(el.clientWidth < max)
    check()
    const ro = new ResizeObserver(check)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, max])
  return narrow
}
