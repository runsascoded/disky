import { createContext, useCallback, useContext, useEffect, useId, useMemo, useReducer, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { MdHelpOutline } from 'react-icons/md'
import { HELP_EMPTY, helpReducer, helpTarget } from './helpState'
import type { HelpEvent, HelpTarget } from './helpState'
import { setHelpPref, useHelpPref } from './prefs'

// The help card (specs/edu-drawer.md): explanations of controls leave the
// floating layer for one fixed card at the bottom-left of the viewport, which
// mirrors whatever control is hovered or keyboard-focused and collapses to a
// chip otherwise. Data tips (cells, rows) stay `<Tooltip>`s.

const HelpCtx = createContext<{ target: HelpTarget | null; send: (e: HelpEvent) => void } | null>(null)

export function HelpProvider({ children }: { children: ReactNode }) {
  const [state, send] = useReducer(helpReducer, HELP_EMPTY)
  const value = useMemo(() => ({ target: helpTarget(state), send }), [state])
  return <HelpCtx.Provider value={value}>{children}</HelpCtx.Provider>
}

/** A control's visible text, for the card's "explaining: …" line. */
const labelOf = (el: HTMLElement): string => (el.querySelector(':scope > .help-tgt')?.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 48)

/** Wrap a control: its explanation shows in the help card while the control
 * is hovered or focused, and is always available to assistive tech through
 * `aria-describedby`. The wrapper lights up (`help-active`) while it is the
 * control the card is explaining, so the card and its target read as a pair.
 * Nothing floats. */
export function Explain({ text, children, className }: { text: ReactNode; children: ReactNode; className?: string }) {
  const ctx = useContext(HelpCtx)
  const [on] = useHelpPref()
  const id = useId()
  const send = ctx?.send
  const target = useCallback((el: HTMLElement): HelpTarget => ({ id, text, label: labelOf(el) }), [id, text])
  const enter = useCallback((e: React.PointerEvent<HTMLElement>) => send?.({ type: 'enter', target: target(e.currentTarget) }), [send, target])
  const leave = useCallback(() => send?.({ type: 'leave', id }), [send, id])
  const focus = useCallback((e: React.FocusEvent<HTMLElement>) => send?.({ type: 'focus', target: target(e.currentTarget) }), [send, target])
  const blur = useCallback(() => send?.({ type: 'blur', id }), [send, id])
  const live = on === 'on' && !!send
  const active = live && ctx?.target?.id === id
  return (
    <span
      className={`has-help${active ? ' help-active' : ''}${className ? ` ${className}` : ''}`}
      aria-describedby={id}
      onPointerEnter={live ? enter : undefined}
      onPointerLeave={live ? leave : undefined}
      onFocus={live ? focus : undefined}
      onBlur={live ? blur : undefined}
    >
      <span className="help-tgt">{children}</span>
      <span id={id} className="sr-only">{text}</span>
    </span>
  )
}

const HELP_INTRO = (
  <>Hover (or keyboard-focus) any control and its explanation shows here, with the control lit up.
  Click anywhere else or press <kbd>Esc</kbd> to collapse this card; <b>snooze</b> (or <kbd>h</kbd>) turns help off until you press <kbd>h</kbd> again.</>
)

/** The help card (specs/edu-drawer.md): a bottom-left panel that shows the
 * hovered/focused control's explanation and names the control. Idle it
 * collapses to a small "?" chip; clicking the chip OPENS the card with a short
 * intro (so a click does something), and the card's × collapses back to the
 * chip. A click on anything that isn't the card or an explained control, or
 * Esc, collapses it too (a focused control otherwise kept it open with no
 * visible reason). `snooze` / `h` turn help off entirely. The card lingers
 * ~1s after the last control so a glance at it isn't a race. */
export function HelpCard() {
  const ctx = useContext(HelpCtx)
  const [on] = useHelpPref()
  const [intro, setIntro] = useState(false)
  const live = ctx?.target ?? null
  const send = ctx?.send
  // Hold the last target briefly after the pointer leaves, so reading the
  // card doesn't clear it (moving off the control to the card would otherwise
  // blank it). A fresh target cancels the pending clear.
  const [shown, setShown] = useState<HelpTarget | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current)
    if (live != null) { setShown(live); return }
    timer.current = setTimeout(() => setShown(null), 1000)
    return () => { if (timer.current) clearTimeout(timer.current) }
  }, [live])
  const open = on === 'on' && (shown != null || intro)
  // Collapse on a click away or Esc: clear the focus that was holding the
  // card open (the hover clears itself when the pointer leaves).
  useEffect(() => {
    if (!open) return
    const collapse = () => { setIntro(false); setShown(null); send?.({ type: 'blur' }) }
    const onDown = (e: PointerEvent) => {
      const el = e.target as Element | null
      if (el?.closest('.help-card, .has-help')) return
      collapse()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') collapse() }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('pointerdown', onDown, true); document.removeEventListener('keydown', onKey) }
  }, [open, send])
  if (on !== 'on') return null
  // What the card shows: a hovered control's text, else the intro if it was
  // opened from the chip, else nothing (collapsed to the chip).
  const body = shown?.text ?? (intro ? HELP_INTRO : null)
  if (body == null) {
    return (
      <button type="button" className="help-chip" onClick={() => setIntro(true)}
        title="What do these controls do?" aria-label="Show help">
        <MdHelpOutline aria-hidden /> help
      </button>
    )
  }
  return (
    <aside className={`help-card${shown ? ' tracking' : ''}`} role="status" aria-live="polite">
      <div className="head">
        <MdHelpOutline aria-hidden /> <span>what this does</span>
        {shown?.label && <span className="tgt" title="the control being explained">· {shown.label}</span>}
        <button type="button" className="snooze" onClick={() => setHelpPref('off')} title="Turn help off (press h to turn it back on)" aria-label="Snooze help">snooze <kbd>h</kbd></button>
        <button type="button" className="x" onClick={() => { setIntro(false); setShown(null); send?.({ type: 'blur' }) }} title="Collapse to the help chip (Esc)" aria-label="Collapse help card">×</button>
      </div>
      <div className="body">{body}</div>
    </aside>
  )
}
