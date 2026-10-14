import { expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { PrefixTable } from './PrefixTable'

it('elides a long prefix while preserving its navigation and full copy value', () => {
  const name = `gs://bucket/${'x'.repeat(100)}/end/`
  const html = renderToStaticMarkup(createElement(MemoryRouter, {}, createElement(PrefixTable, { rows: [{ name, to: '/bucket/key' }], sort: { k: 'name', asc: true }, onSort: () => {} })))
  expect([...html.matchAll(/<code>([^<]+)<\/code>/g)].map(m => m[1])).toEqual([`gs://bucket/${'x'.repeat(45)}…${'x'.repeat(23)}/end/`])
  expect([...html.matchAll(/href="([^"]+)"/g)].map(m => m[1])).toEqual(['/bucket/key'])
  expect([...html.matchAll(/aria-label="([^"]+)"/g)].map(m => m[1])).toEqual([`Copy ${name}`])
})
