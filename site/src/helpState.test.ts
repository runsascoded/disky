import { describe, expect, it } from 'vitest'
import { HELP_EMPTY, helpReducer, helpTarget, helpText } from './helpState'
import type { HelpEvent, HelpState, HelpTarget } from './helpState'

const run = (events: HelpEvent[], from: HelpState = HELP_EMPTY): HelpState => events.reduce(helpReducer, from)
const A: HelpTarget = { id: 'a', text: 'about a', label: 'A' }
const B: HelpTarget = { id: 'b', text: 'about b', label: 'B' }

describe('helpReducer', () => {
  it('enter/leave set and clear the hovered control', () => {
    expect(run([{ type: 'enter', target: A }])).toEqual({ hover: A, focus: null })
    expect(run([{ type: 'enter', target: A }, { type: 'leave', id: 'a' }])).toEqual(HELP_EMPTY)
  })
  it('a leave for a control that is no longer hovered is ignored (crossed events)', () => {
    const s = run([{ type: 'enter', target: A }, { type: 'enter', target: B }, { type: 'leave', id: 'a' }])
    expect(s).toEqual({ hover: B, focus: null })
  })
  it('a leave after a focus keeps the focused control; its blur clears it', () => {
    const s = run([{ type: 'focus', target: A }, { type: 'enter', target: B }, { type: 'leave', id: 'b' }])
    expect(s).toEqual({ hover: null, focus: A })
    expect(helpTarget(s)).toBe(A)
    expect(helpText(s)).toBe('about a')
    expect(run([{ type: 'blur', id: 'a' }], s)).toEqual(HELP_EMPTY)
  })
  it('a blur naming another control is ignored; a bare blur (click away, Esc) clears any focus', () => {
    const s = run([{ type: 'focus', target: A }])
    expect(run([{ type: 'blur', id: 'b' }], s)).toBe(s)
    expect(run([{ type: 'blur' }], s)).toEqual(HELP_EMPTY)
  })
  it('the hovered control wins over the focused one; blur falls back to the hover', () => {
    const s = run([{ type: 'focus', target: A }, { type: 'enter', target: B }])
    expect(helpTarget(s)).toBe(B)
    expect(helpTarget(run([{ type: 'blur', id: 'a' }], s))).toBe(B)
  })
  it('no-op events return the same state object', () => {
    const s = run([{ type: 'enter', target: A }])
    expect(helpReducer(s, { type: 'enter', target: { ...A } })).toBe(s)
    expect(helpReducer(HELP_EMPTY, { type: 'leave', id: 'a' })).toBe(HELP_EMPTY)
    expect(helpReducer(HELP_EMPTY, { type: 'blur' })).toBe(HELP_EMPTY)
  })
  it('nothing hovered or focused shows nothing', () => {
    expect(helpTarget(HELP_EMPTY)).toBeNull()
    expect(helpText(HELP_EMPTY)).toBeNull()
  })
})
