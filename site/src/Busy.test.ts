import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { Loading } from './Busy'

it('renders a visible spinner and announces the specific pending action', () => {
  expect(renderToStaticMarkup(createElement(Loading, { label: 'Loading prefix outcomes…' }))).toBe(
    '<span class="inline-loading" role="status" aria-live="polite"><span class="spin" aria-hidden="true"></span>Loading prefix outcomes…</span>',
  )
})
