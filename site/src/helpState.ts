// The help card's state (specs/edu-drawer.md §1): one explanation at a time,
// from whatever control is hovered or keyboard-focused. Pure — the provider
// in Help.tsx drives it from pointer/focus events.

import type { ReactNode } from 'react'

/** One explained control: its stable id (so the control can tell it is the
 * active one and light up), its explanation, and its visible label (so the
 * card can say *what* it is explaining). */
export interface HelpTarget {
  id: string
  text: ReactNode
  /** The control's own text, trimmed; '' when it has none (an icon). */
  label: string
}

export interface HelpState {
  /** The hovered control (null = pointer over nothing explained). */
  hover: HelpTarget | null
  /** The focused control (null = nothing explained has focus). */
  focus: HelpTarget | null
}

export type HelpEvent =
  | { type: 'enter'; target: HelpTarget }
  | { type: 'leave'; id: string }
  | { type: 'focus'; target: HelpTarget }
  | { type: 'blur'; id?: string }

export const HELP_EMPTY: HelpState = { hover: null, focus: null }

/** Hover and focus are tracked apart so leaving a control keeps a focused
 * one's text, and blurring falls back to whatever is still hovered. A leave
 * or blur names its control, so a stale one (events can cross when the
 * pointer jumps between controls) never clears a newer one; a bare blur
 * (a click on empty page, Esc) clears any focus. */
export function helpReducer(s: HelpState, e: HelpEvent): HelpState {
  switch (e.type) {
    case 'enter': return s.hover?.id === e.target.id ? s : { ...s, hover: e.target }
    case 'leave': return s.hover === null || s.hover.id !== e.id ? s : { ...s, hover: null }
    case 'focus': return s.focus?.id === e.target.id ? s : { ...s, focus: e.target }
    case 'blur': return s.focus === null || (e.id != null && s.focus.id !== e.id) ? s : { ...s, focus: null }
  }
}

/** What the card shows: the hovered control wins (it is where the pointer
 * is), else the focused one, else nothing. */
export const helpTarget = (s: HelpState): HelpTarget | null => s.hover ?? s.focus
export const helpText = (s: HelpState): ReactNode | null => helpTarget(s)?.text ?? null
