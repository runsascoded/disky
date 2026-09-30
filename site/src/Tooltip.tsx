import {
  FloatingPortal,
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
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import type { Placement } from '@floating-ui/react'
import { CLASS_COLORS, CLASS_NAMES, CLASS_PRICE_US, fmtUsd } from './types'
import { useUnits } from './units'

/** Generic hover/focus tooltip (@floating-ui/react); replaces native title=. */
export function Tooltip({ content, children, placement = 'top', pinnable }: {
  content: React.ReactNode
  children: React.ReactNode
  placement?: Placement
  /** Click the reference to PIN the tip open (interact with it; Esc or a
   * click elsewhere releases). Click again to release. */
  pinnable?: boolean
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
    placement,
    // `size` caps the tip to the viewport (minus padding): `shift` only moves a
    // tip along its cross axis, so a wide tip placed left/right of a reference
    // near the edge would otherwise run off-screen on a phone.
    middleware: [offset(6), flip(), shift({ padding: 8 }), size({ padding: 8, apply: ({ availableWidth, elements }) => { elements.floating.style.maxWidth = `${Math.min(420, availableWidth)}px` } })],
    whileElementsMounted: autoUpdate,
  })
  // `safePolygon`: the tip stays while the pointer travels into it, so its
  // contents (copy buttons, names) are reachable.
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useHover(context, { move: false, delay: { open: 80 }, handleClose: safePolygon() }),
    // `visibleOnly`: keyboard focus opens the tip, a click's focus doesn't — a
    // <select> keeps focus after a choice, which pinned its tip open (and, the
    // open state then being focus-owned, hover no longer toggled it).
    useFocus(context, { visibleOnly: true }),
    useDismiss(context, { outsidePress: true }),
    useRole(context, { role: 'tooltip' }),
  ])
  const refProps = getReferenceProps(pinnable ? { onClick: () => setPinned(p => !p) } : {})
  return (
    <>
      <span className={`tt-ref${pinnable ? ' pinnable' : ''}${pinned ? ' pinned' : ''}`} ref={refs.setReference} tabIndex={0} {...refProps}>
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
