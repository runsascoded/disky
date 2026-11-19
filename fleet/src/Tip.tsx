/** The page's tooltips (`@floating-ui/react`): `Tip` on an element (hover or focus opens it), and `PointTip` at a point
 *  inside an SVG chart (the chart says which point is hovered). Both portal to the body, so a scroll container never clips
 *  them, and flip / shift to stay on screen at phone width. */
import {
  autoUpdate, flip, FloatingPortal, offset, shift, useDismiss, useFloating, useFocus, useHover, useInteractions, useRole,
} from '@floating-ui/react'
import { cloneElement, type ReactElement, type ReactNode, useLayoutEffect, useState } from 'react'

const MIDDLEWARE = [offset(8), flip({ padding: 8 }), shift({ padding: 8 })]

export function Tip({ content, children }: { content: ReactNode; children: ReactElement<Record<string, unknown>> }) {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({ open, onOpenChange: setOpen, placement: 'top', middleware: MIDDLEWARE, whileElementsMounted: autoUpdate })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useHover(context, { delay: { open: 60, close: 0 }, move: false }),
    useFocus(context),
    useDismiss(context),
    useRole(context, { role: 'tooltip' }),
  ])
  return (
    <>
      {cloneElement(children, { ref: refs.setReference, ...getReferenceProps(children.props) })}
      {open && (
        <FloatingPortal>
          <div className="tip" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>{content}</div>
        </FloatingPortal>
      )}
    </>
  )
}

/** A tooltip at `(x, y)` in `svg`'s own pixels, while `at` is set. */
export function PointTip({ svg, at, children }: { svg: SVGSVGElement | null; at: { x: number; y: number } | null; children: ReactNode }) {
  const { refs, floatingStyles } = useFloating({ open: !!at, placement: 'top', middleware: MIDDLEWARE, whileElementsMounted: autoUpdate })
  useLayoutEffect(() => {
    if (!svg || !at) return
    refs.setPositionReference({
      contextElement: svg,
      getBoundingClientRect: () => {
        const r = svg.getBoundingClientRect()
        return new DOMRect(r.left + at.x, r.top + at.y - 4, 0, 0)
      },
    })
  }, [svg, at?.x, at?.y, refs])
  if (!at) return null
  return (
    <FloatingPortal>
      <div className="tip" role="tooltip" ref={refs.setFloating} style={floatingStyles}>{children}</div>
    </FloatingPortal>
  )
}
