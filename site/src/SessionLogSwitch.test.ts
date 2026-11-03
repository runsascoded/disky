// `/admin`'s session-log switch (specs/session-log.md): the markup per state — off (never set), on (with who set it),
// on with no end date (including the hand-written 2100-01-01 end), expired, a changed end while on (the "Set end"
// button), and a deployment without the migration (no form).
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { FOREVER_MS, type SwitchState } from '../functions/_lib/sessionLogShape'
import { SessionLogSwitchView, type SwitchViewProps } from './SessionLogSwitch'

const bounds = { defaultUntil: Date.UTC(2026, 9, 20), maxUntil: Date.UTC(2026, 10, 13) }
const OFF: SwitchState = { enabled: false, until: null, indefinite: false, reason: 'off', who: null, ts: null, ...bounds }
const ON: SwitchState = { enabled: true, until: Date.UTC(2026, 9, 15), indefinite: false, reason: null, who: 'ops@example.test', ts: Date.UTC(2026, 9, 12, 15) / 1000, ...bounds }

const render = (state: SwitchState, o: Partial<SwitchViewProps> = {}) => renderToStaticMarkup(createElement(MemoryRouter, null,
  createElement(SessionLogSwitchView, { state, today: '2026-10-12', date: '2026-10-19', onDate: () => {}, end: 'dated', onEnd: () => {}, onSet: () => {}, busy: false, error: null, ...o })))

const intro = '<h2>Session log</h2><p>Records what viewers do (page views, clicks on controls, the path filter, requests and errors), to replay a reported bug (<a href="/admin/sessions">/admin/sessions</a>) and for the activity feed (<a href="/admin/activity">/admin/activity</a>). While it&#x27;s on, <a href="/privacy">/privacy</a> says what is recorded. It turns itself off after the chosen day, or runs until switched off with no end date.</p>'
const SET = '<button type="submit">Set end</button>'
const radio = (on: boolean) => `<input type="radio" name="session-log-end"${on ? ' checked=""' : ''}/>`
const form = (checked: boolean, date: string, extra = '', indefinite = false) =>
  `<form><label class="switch"><input type="checkbox" role="switch"${checked ? ' checked=""' : ''}/> Logging</label>`
  + `<span class="ends" role="radiogroup" aria-label="Ends">`
  + `<label>${radio(!indefinite)} through <input type="date" min="2026-10-12" max="2026-11-12" value="${date}"/> (UTC)</label>`
  + `<label>${radio(indefinite)} no end date</label></span>${extra}</form>`

describe('the /admin session-log switch', () => {
  it('off (never set): unchecked, the date defaulting to 7 days out', () => {
    expect(render(OFF)).toBe(`<section class="session-log-switch">${intro}<p class="status">Off.</p>${form(false, '2026-10-19')}</section>`)
  })
  it('on: checked, through its last day, who set it', () => {
    expect(render(ON, { date: '2026-10-14' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On through 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-14')}</section>`,
    )
  })
  it('on, with the date changed: a "Set end" button', () => {
    expect(render(ON, { date: '2026-10-20' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On through 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-20', SET)}</section>`,
    )
  })
  it('on, "no end date" picked: a "Set end" button', () => {
    expect(render(ON, { date: '2026-10-14', end: 'indefinite' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On through 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-14', SET, true)}</section>`,
    )
  })
  it('on with no end date (the sentinel, or the hand-written 2100-01-01): "On (no end date)", no button until a date is picked', () => {
    for (const until of [FOREVER_MS, 4102444800000]) {
      expect(render({ ...ON, until, indefinite: true }, { end: 'indefinite' })).toBe(
        `<section class="session-log-switch">${intro}<p class="status">On (no end date). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-19', '', true)}</section>`,
      )
    }
    expect(render({ ...ON, until: FOREVER_MS, indefinite: true }, { end: 'dated' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On (no end date). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-19', SET)}</section>`,
    )
  })
  it('expired: off, saying when it ended; an error shows under the form', () => {
    expect(render({ ...ON, enabled: false, reason: 'expired' }, { error: 'until 2026-11-13 is more than 31 days ahead (latest: 2026-11-12)' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">Off: ended after 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(false, '2026-10-19')}`
      + '<p class="err">until 2026-11-13 is more than 31 days ahead (latest: 2026-11-12)</p></section>',
    )
  })
  it('not set up (no migration): no form', () => {
    expect(render({ ...OFF, reason: 'not set up' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">Not set up on this deployment (no session_log tables): apply the session-log migration.</p></section>`,
    )
  })
})
