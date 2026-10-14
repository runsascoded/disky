// `/admin`'s session-log switch (specs/session-log.md): the markup per state — off (never set), on (with who set it),
// expired, a changed date while on (the "Set date" button), and a deployment without the migration (no form).
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { SwitchState } from '../functions/_lib/sessionLogShape'
import { SessionLogSwitchView, type SwitchViewProps } from './SessionLogSwitch'

const bounds = { defaultUntil: Date.UTC(2026, 9, 20), maxUntil: Date.UTC(2026, 10, 13) }
const OFF: SwitchState = { enabled: false, until: null, reason: 'off', who: null, ts: null, ...bounds }
const ON: SwitchState = { enabled: true, until: Date.UTC(2026, 9, 15), reason: null, who: 'ops@example.test', ts: Date.UTC(2026, 9, 12, 15) / 1000, ...bounds }

const render = (state: SwitchState, o: Partial<SwitchViewProps> = {}) => renderToStaticMarkup(createElement(MemoryRouter, null,
  createElement(SessionLogSwitchView, { state, today: '2026-10-12', date: '2026-10-19', onDate: () => {}, onSet: () => {}, busy: false, error: null, ...o })))

const intro = '<h2>Session log</h2><p>Records what viewers do (page views, clicks on controls, the path filter, requests and errors) so a reported bug can be replayed: <a href="/admin/sessions">/admin/sessions</a>. Viewers see no notice. It turns itself off after the chosen day.</p>'
const form = (checked: boolean, date: string, extra = '') =>
  `<form><label class="switch"><input type="checkbox" role="switch"${checked ? ' checked=""' : ''}/> Logging</label>`
  + `<label> through <input type="date" min="2026-10-12" max="2026-11-12" value="${date}"/> (UTC)</label>${extra}</form>`

describe('the /admin session-log switch', () => {
  it('off (never set): unchecked, the date defaulting to 7 days out', () => {
    expect(render(OFF)).toBe(`<section class="session-log-switch">${intro}<p class="status">Off.</p>${form(false, '2026-10-19')}</section>`)
  })
  it('on: checked, through its last day, who set it', () => {
    expect(render(ON, { date: '2026-10-14' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On through 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-14')}</section>`,
    )
  })
  it('on, with the date changed: a "Set date" button', () => {
    expect(render(ON, { date: '2026-10-20' })).toBe(
      `<section class="session-log-switch">${intro}<p class="status">On through 2026-10-14 (UTC). Set by ops@example.test on 2026-10-12.</p>${form(true, '2026-10-20', '<button type="submit">Set date</button>')}</section>`,
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
