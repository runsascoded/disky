import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { RunChecks } from './RunChecks'

function states(checks: Parameters<typeof RunChecks>[0]['checks'], live: boolean) {
  const html = renderToStaticMarkup(createElement(RunChecks, { checks, live }))
  return [...html.matchAll(/<li[^>]*class="([^"]+)"[^>]*><span class="check-status">([^<]+)<\/span>/g)].map(m => [m[1], m[2]])
}

it('keeps all six checks pending until their final inputs exist', () => {
  expect(states([], true)).toEqual(Array.from({ length: 6 }, () => ['pending', '◷ pending']))
})

it('renders passed, mismatched, and missing checks distinctly', () => {
  expect(states([
    { label: 'deleted objects: log = D1', expected: 10, actual: 10, ok: true },
    { label: 'deleted bytes: log = D1', expected: 100, actual: 90, ok: false },
  ], false)).toEqual([
    ['ok', '✓ passed'], ['off', '≠ mismatch'],
    ['pending', '◷ pending'], ['pending', '◷ pending'], ['pending', '◷ pending'], ['pending', '◷ pending'],
  ])
})
