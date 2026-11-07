import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/**
 * Headless hover+pin state machine — the common core of the legend/map/cell
 * "pin" UX. Ported verbatim from marin-gcs-usage's `hoverpin/useHoverPin.ts`
 * (itself distilled from prior implementations in ctbk's StationMap, hbt's
 * GeoSankey, pltly's useTraceHighlight):
 *
 * - `active = pinned ?? hovered`; single pin; click toggles (re-click unpins,
 *   click elsewhere switches the pin).
 * - Touch devices (`(hover: none)`) ignore hover entirely — tap-toggle is the
 *   only interaction, sidestepping the hover→click→unhover race.
 * - Re-click-unpin suppresses that key's hover until the pointer truly leaves
 *   it, so unpinning doesn't instantly re-highlight.
 * - Outside-click clears the pin, scoped by `excludeRefs`/`excludeSelectors`
 *   (clicks inside those never count as "outside"). Escape also clears.
 *
 * No dependencies beyond React; keys are opaque (compared by Object.is).
 */
export interface HoverPin<K> {
  hovered: K | null
  pinned: K | null
  /** pinned ?? hovered — what the UI should treat as focused */
  active: K | null
  /** pointerenter/move with a key; pointerleave with null */
  hover: (k: K | null) => void
  /** click on the keyed element: pin / unpin-if-same / switch */
  togglePin: (k: K) => void
  clearPin: () => void
}

export interface HoverPinOpts {
  /** clicks inside these elements never clear the pin */
  excludeRefs?: React.RefObject<Element | null>[]
  /** clicks inside elements matching any selector never clear the pin */
  excludeSelectors?: string[]
}

export function useHoverPin<K>({ excludeRefs = [], excludeSelectors = [] }: HoverPinOpts = {}): HoverPin<K> {
  const [hovered, setHovered] = useState<K | null>(null)
  const [pinned, setPinned] = useState<K | null>(null)
  const suppressed = useRef<K | null>(null)
  const touch = useMemo(
    () => typeof window !== 'undefined' && window.matchMedia?.('(hover: none)').matches,
    [],
  )

  const hover = useCallback(
    (k: K | null) => {
      if (touch) return
      if (k === null || !Object.is(k, suppressed.current)) suppressed.current = null
      setHovered(Object.is(k, suppressed.current) ? null : k)
    },
    [touch],
  )

  const togglePin = useCallback((k: K) => {
    setPinned(prev => {
      if (Object.is(prev, k)) {
        suppressed.current = k
        setHovered(h => (Object.is(h, k) ? null : h))
        return null
      }
      return k
    })
  }, [])

  const clearPin = useCallback(() => setPinned(null), [])

  useEffect(() => {
    if (pinned === null) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element | null
      if (!t) return
      if (excludeRefs.some(r => r.current?.contains(t))) return
      if (excludeSelectors.some(sel => t.closest(sel))) return
      setPinned(null)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPinned(null)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
    // refs/selectors are stable per callsite
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinned])

  return { hovered, pinned, active: pinned ?? hovered, hover, togglePin, clearPin }
}
