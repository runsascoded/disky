import { useLayoutEffect, useState } from 'react'

/** An element's width in CSS px (`fallback` until it mounts), through a callback ref, so an element that mounts after
 *  its component (behind a loading state) is still measured. Measured before observing: a hidden tab never gets the
 *  first ResizeObserver callback. */
export function useWidth<T extends HTMLElement>(fallback = 720): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null)
  const [w, setW] = useState(fallback)
  useLayoutEffect(() => {
    if (!el) return
    setW(el.clientWidth || fallback)
    const ro = new ResizeObserver(() => setW(el.clientWidth || fallback))
    ro.observe(el)
    return () => ro.disconnect()
  }, [el, fallback])
  return [setEl, w]
}
