import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { expect, it } from 'vitest'
import { PrefixRecovery } from './PrefixRecovery'

it('shows preserved objects beside the recorded deletion count, with a run-detail link', () => {
  const markup = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(PrefixRecovery, {
    execution: { kind: 'recorded', runId: 'real-run', objects: 5, bytes: 50, preserved: 10, undone: 0, deadline: null, expired: false },
    fmtBytes: bytes => `${bytes} B`,
  })))
  expect(markup.replace(/<[^>]*>/g, '')).toBe('5 logged deleted · 10 changed/new preserved · details')
  expect([...markup.matchAll(/href="([^"]*)"/g)].map(m => m[1])).toEqual(['/runs/real-run#recovery'])
})
