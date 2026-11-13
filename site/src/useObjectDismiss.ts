import { useEffect, type RefObject } from 'react'

/** The drawer owns its contents; map clicks may choose another object. Other primary clicks dismiss it. */
export function useObjectDismiss(ref: RefObject<HTMLElement | null>, onClose: () => void): void {
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0) return
      const target = e.target
      if (!(target instanceof Element) || ref.current?.contains(target) || target.closest('.dt-treemap-map')) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if ((e.target as Element | null)?.closest('input, textarea, select, [contenteditable="true"]')) return
      // Consume dismissal before the map's window handler interprets Escape as parent navigation.
      e.preventDefault()
      onClose()
    }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [ref, onClose])
}
