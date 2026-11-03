// `/admin`'s activity table and `/admin/activity`'s pieces (specs/done/activity-feed.md): a kind's row (changed from its
// default, record off greys the notify select), the posting line, the counts line and code spans in event lines.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ActivityKinds, countsText, Line } from './ActivityKinds'

const kinds = {
  kinds: [
    { kind: 'assign', source: 'server', covers: 'Owner assigned', default: { log: true, notify: 'line' }, log: true, notify: 'line' },
    { kind: 'click', source: 'client', covers: 'Clicks', default: { log: true, notify: 'count' }, log: false, notify: 'off' },
  ],
  sinks: { slack: true, discord: false },
}

const render = () => {
  const qc = new QueryClient()
  qc.setQueryData(['activity', 'kinds'], kinds)
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(QueryClientProvider, { client: qc }, createElement(ActivityKinds))))
}

const options = (selected: string) => ['off', 'count', 'line'].map(n => `<option value="${n}"${n === selected ? ' selected=""' : ''}>${n === 'line' ? 'each one' : n}</option>`).join('')

describe('the /admin activity table', () => {
  it('rows per kind: changed ones starred, unrecorded ones with the notify select off', () => {
    const html = render()
    const rows = html.slice(html.indexOf('<tbody>') + 7, html.indexOf('</tbody>'))
    expect(rows.split('</tr>').filter(Boolean)).toEqual([
      '<tr class=""><td><code>assign</code></td><td>Owner assigned</td>'
      + '<td><input type="checkbox" aria-label="record assign" checked=""/></td>'
      + `<td><select aria-label="report assign">${options('line')}</select></td>`,
      '<tr class="off"><td><code>click</code><span class="dim" title="default: recorded, count"> *</span></td><td>Clicks<span class="dim"> (session log)</span></td>'
      + '<td><input type="checkbox" aria-label="record click"/></td>'
      + `<td><select aria-label="report click" disabled="">${options('off')}</select></td>`,
    ])
    expect(html.includes(' Posting to: Slack.</p>')).toBe(true)
  })
})

describe('activity lines', () => {
  it('counts in report order', () => {
    expect(countsText({ click: 3, assign: 1, view: 2, nope: 0 })).toBe('1 assignment · 2 page loads · 3 clicks')
  })
  it('code spans render as code', () => {
    expect(renderToStaticMarkup(createElement(Line, { text: '`gs://b/x/` (1.0 GiB) → `bob`' }))).toBe('<code>gs://b/x/</code> (1.0 GiB) → <code>bob</code>')
  })
})
