import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RunOutcome, runOutcomeLabel } from './RunOutcome'

describe('run outcomes distinguish predictions from actual deletions', () => {
  it('dry-run totals visibly say would delete, independently of row styling', () => {
    expect(renderToStaticMarkup(createElement(RunOutcome, { mode: 'dry', children: '1 Ti · 10' }))).toBe(
      '<span class="run-outcome dry"><span class="prediction-label">would delete</span>1 Ti · 10</span>',
    )
  })
  it('real totals are not labelled as predictions', () => {
    expect(renderToStaticMarkup(createElement(RunOutcome, { mode: 'real', children: '1 Ti · 10' }))).toBe(
      '<span class="run-outcome real">1 Ti · 10</span>',
    )
  })
  it('zero predictions remain explicitly hypothetical', () => {
    expect(renderToStaticMarkup(createElement(RunOutcome, { mode: 'dry', children: '0 B · 0' }))).toBe(
      '<span class="run-outcome dry"><span class="prediction-label">would delete</span>0 B · 0</span>',
    )
  })
  it('detail and rate labels use the same mode vocabulary', () => {
    expect(['dry', 'real'].map(mode => runOutcomeLabel(mode as 'dry' | 'real'))).toEqual(['would delete', 'deleted'])
  })
})
