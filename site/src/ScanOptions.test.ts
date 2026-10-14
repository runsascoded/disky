import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { ScanOptions } from './ScanCombobox'

it('a one-scan day is one option (day, then its time dimmed); a several-scan day is a header over its times', () => {
  const groups = [
    { day: '10/12', scans: [{ id: '2026-10-12T1802', label: '2:02p' }, { id: '2026-10-12T0601', label: '2:01a' }] },
    { day: '10/11', scans: [{ id: '2026-10-11', label: '8:01a' }] },
    { day: '10/10', scans: [{ id: '2026-10-10', label: '10/10' }] },
  ]
  const html = renderToStaticMarkup(createElement(ScanOptions, {
    groups, value: '2026-10-11', activeId: '2026-10-12T0601', optId: (id: string) => `o-${id}`, onPick: () => {}, onHover: () => {},
  }))
  expect(html).toBe(
    '<li role="presentation" class="sp-group"><div class="sp-day" role="presentation">10/12</div><ul role="presentation">'
    + '<li id="o-2026-10-12T1802" role="option" aria-selected="false" class="sp-opt">2:02p</li>'
    + '<li id="o-2026-10-12T0601" role="option" aria-selected="false" class="sp-opt active">2:01a</li>'
    + '</ul></li>'
    + '<li id="o-2026-10-11" role="option" aria-selected="true" class="sp-opt solo sel">10/11<span class="sp-time">8:01a</span></li>'
    + '<li id="o-2026-10-10" role="option" aria-selected="false" class="sp-opt solo">10/10</li>',
  )
})
