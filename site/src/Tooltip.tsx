import {
  FloatingPortal,
  NextFloatingDelayGroup,
  autoUpdate,
  flip,
  offset,
  safePolygon,
  shift,
  size,
  useDismiss,
  useFloating,
  useFocus,
  useHover,
  useInteractions,
  useNextDelayGroup,
  useRole,
} from '@floating-ui/react'
import { useCallback, useState } from 'react'
import type { Placement } from '@floating-ui/react'
import { CLASS_COLORS, CLASS_NAMES, CLASS_PRICE_US, fmtUsd } from './types'
import { useUnits } from './units'

/** The hover-intent delay before a tip opens, when no neighboring tip was just open. */
const OPEN_DELAY = 80

/** App-root provider (`main.tsx`): every `<Tooltip>` shares one delay group, so once a tip is open, moving onto
 *  another opens it at once and closes the first — scanning a column of tips swaps them with no closed gap and
 *  no re-wait. The grace window (`timeoutMs`) covers the pointer crossing the gap between two references. Uses
 *  the ref-based `NextFloatingDelayGroup`: a delay change doesn't re-render every tip on the page (tables carry
 *  thousands). */
export function TooltipGroup({ children }: { children: React.ReactNode }) {
  return <NextFloatingDelayGroup delay={{ open: OPEN_DELAY, close: 0 }} timeoutMs={300}>{children}</NextFloatingDelayGroup>
}

/**
 * Generic hover/focus tooltip (@floating-ui/react); replaces native title=.
 *
 * **In a table cell, pass `cell`.** The hover target is then the enclosing `<td>`/`<th>` (not the inline span
 * around the text), so a column of tips has flush targets: no dead gaps between rows (where the pointer
 * closes one tip and has to wait out the open delay for the next), and the whole cell responds. `cell` also
 * places the tip to the `right` by default: a `top` tip covers the row above, i.e. the very target the
 * pointer moves to next when scanning up the column. With `<TooltipGroup>` at the root, neighbors swap
 * instantly. Outside a cell, `cell` falls back to the inline span.
 */
export function Tooltip({ content, children, placement, pinnable, cell }: {
  content: React.ReactNode
  children: React.ReactNode
  placement?: Placement
  /** Click the reference to PIN the tip open (interact with it; Esc or a
   * click elsewhere releases). Click again to release. */
  pinnable?: boolean
  /** Hover target = the enclosing table cell; default placement `right` (see above). */
  cell?: boolean
}) {
  const [hoverOpen, setHoverOpen] = useState(false)
  const [pinned, setPinned] = useState(false)
  const open = hoverOpen || pinned
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: (o, _e, reason) => {
      // A dismiss (outside press / Esc) releases a pin; hover changes never do.
      if (!o && pinned && (reason === 'outside-press' || reason === 'escape-key')) setPinned(false)
      setHoverOpen(o)
    },
    placement: placement ?? (cell ? 'right' : 'top'),
    // `size` caps the tip to the viewport (minus padding): `shift` only moves a
    // tip along its cross axis, so a wide tip placed left/right of a reference
    // near the edge would otherwise run off-screen on a phone.
    middleware: [offset(6), flip(), shift({ padding: 8 }), size({ padding: 8, apply: ({ availableWidth, elements }) => { elements.floating.style.maxWidth = `${Math.min(420, availableWidth)}px` } })],
    whileElementsMounted: autoUpdate,
  })
  // The root `<TooltipGroup>`'s delay: `OPEN_DELAY`, or 0 right after another tip (no provider: a lone tip).
  const { delayRef, hasProvider } = useNextDelayGroup(context)
  // `safePolygon`: the tip stays while the pointer travels into it, so its
  // contents (copy buttons, names) are reachable.
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useHover(context, { move: false, delay: hasProvider ? () => delayRef.current : { open: OPEN_DELAY }, handleClose: safePolygon() }),
    // `visibleOnly`: keyboard focus opens the tip, a click's focus doesn't — a
    // <select> keeps focus after a choice, which pinned its tip open (and, the
    // open state then being focus-owned, hover no longer toggled it).
    useFocus(context, { visibleOnly: true }),
    useDismiss(context, { outsidePress: true }),
    useRole(context, { role: 'tooltip' }),
  ])
  // `cell`: position and hover-listen on the enclosing cell (useHover's enter/leave listeners are native, on
  // the positioning reference); focus and the pin click stay on the span. Stable, so React doesn't re-attach it.
  const setRef = useCallback((el: HTMLSpanElement | null) => {
    refs.setReference(cell && el ? el.closest('td, th') ?? el : el)
  }, [cell, refs])
  const refProps = getReferenceProps(pinnable ? { onClick: () => setPinned(p => !p) } : {})
  return (
    <>
      <span className={`tt-ref${pinnable ? ' pinnable' : ''}${pinned ? ' pinned' : ''}`} ref={setRef} tabIndex={0} {...refProps}>
        {children}
      </span>
      {open && (
        <FloatingPortal>
          <div className={`tooltip-content${pinned ? ' pinned' : ''}`} ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
            {content}
            {pinnable && <div className="pin-hint">{pinned ? 'pinned · Esc or click away to close' : 'click to pin'}</div>}
          </div>
        </FloatingPortal>
      )}
    </>
  )
}

/** use-kbd `SpeedDial` `TooltipRenderer`: the same floating tip, anchored to
 * the hovered dial button (use-kbd owns hover detection and mounts this only
 * while a button is hovered), placed to the dial's left so it never leaves the
 * viewport. Replaces the native `title=` the dial falls back to. */
export function SpeedDialTip({ title, anchor }: { title: string; anchor: HTMLElement }) {
  const { refs, floatingStyles } = useFloating({
    open: true,
    elements: { reference: anchor },
    placement: 'left',
    middleware: [offset(8), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  return (
    <FloatingPortal>
      <div className="tooltip-content" ref={refs.setFloating} style={floatingStyles} role="tooltip">{title}</div>
    </FloatingPortal>
  )
}

/** Per-class bytes/rate/$ breakdown table for a class-byte mix. */
export function ClassMixTip({ mix, note }: { mix: Record<string, number>; note?: string }) {
  const { fmtBytes } = useUnits()
  const rows = Object.entries(mix)
    .filter(([, b]) => b > 0)
    .sort((a, b) => b[1] - a[1])
  const total = rows.reduce((s, [c, b]) => s + (b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02), 0)
  return (
    <div className="classmix">
      <table>
        <tbody>
          {rows.map(([c, b]) => (
            <tr key={c}>
              <td><i className="sw" style={{ background: CLASS_COLORS[c] ?? 'var(--other)' }} />{CLASS_NAMES[c] ?? c}</td>
              <td className="num">{fmtBytes(b)}</td>
              <td className="num">${CLASS_PRICE_US[c] ?? 0.02}/GiB</td>
              <td className="num">{fmtUsd((b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02))}/mo</td>
            </tr>
          ))}
          {rows.length > 1 && (
            <tr className="tot">
              <td>total</td>
              <td className="num">{fmtBytes(rows.reduce((s, [, b]) => s + b, 0))}</td>
              <td />
              <td className="num">{fmtUsd(total)}/mo</td>
            </tr>
          )}
        </tbody>
      </table>
      {note && <div className="note">{note}</div>}
    </div>
  )
}
