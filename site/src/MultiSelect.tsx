import {
  FloatingFocusManager,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Tooltip } from './Tooltip'

export interface MsOption<K extends string> {
  key: K
  label: string
  glyph?: string
  color?: string
  tip?: ReactNode
}

// A filter over a small fixed set (the bar's mark and owner axes): one button
// that states the current selection ("all", or the kept entries), opening a
// checklist with an "only" shortcut per row. Every row on = no filter; the
// last row can't be switched off (that would show nothing) — it rolls back
// to all. Reads as a filter, not as a row of actions.
export function MultiSelect<K extends string>({ label, options, selected, onChange, disabled }: {
  label: string
  options: MsOption<K>[]
  selected: readonly K[]
  onChange: (keys: K[]) => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: 'bottom-start',
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useClick(context),
    useDismiss(context),
    useRole(context, { role: 'listbox' }),
  ])
  const keys = options.map(o => o.key)
  const on = new Set(selected)
  const all = keys.every(k => on.has(k))
  const toggle = (k: K) => {
    const next = keys.filter(x => (x === k ? !on.has(x) : on.has(x)))
    onChange(next.length === 0 ? keys : next)
  }
  const row = (o: MsOption<K>) => (
    <label className="ms-lbl">
      <input type="checkbox" checked={on.has(o.key)} onChange={() => toggle(o.key)} />
      {o.glyph && <span className="ms-glyph" aria-hidden>{o.glyph}</span>}
      {o.label}
    </label>
  )
  return (
    <>
      <button
        type="button"
        className={`tb-select ms-btn${all ? '' : ' narrowed'}`}
        ref={refs.setReference}
        {...getReferenceProps()}
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
      >
        {all
          ? <span className="ms-all">all</span>
          : options.filter(o => on.has(o.key)).map(o => (
            <span key={o.key} className="ms-sel" style={{ color: o.color }}>
              {o.glyph && <span className="ms-glyph" aria-hidden>{o.glyph}</span>}{o.label}
            </span>
          ))}
        <span className="ms-caret" aria-hidden>▾</span>
      </button>
      {open && (
        <FloatingPortal>
          <FloatingFocusManager context={context} modal={false}>
            <div className="menu-pop ms-pop" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
              <label className="ms-row ms-allrow">
                <span className="ms-lbl">
                  <input type="checkbox" checked={all} disabled={all} onChange={() => onChange(keys)} />
                  all {label}
                </span>
              </label>
              {options.map(o => (
                <div key={o.key} className={`ms-row${on.has(o.key) ? ' on' : ''}`} style={{ '--kind': o.color } as CSSProperties}>
                  {o.tip ? <Tooltip content={o.tip}>{row(o)}</Tooltip> : row(o)}
                  <button type="button" className="ms-only" onClick={() => onChange([o.key])} title={`Only ${o.label}`}>only</button>
                </div>
              ))}
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      )}
    </>
  )
}
