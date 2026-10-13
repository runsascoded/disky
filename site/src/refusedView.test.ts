import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { apiError } from './filterCaps'
import { RootCrumb } from './RootCrumb'
import { SeriesEmpty } from './SeriesEmpty'

// A filtered view the server refuses (a structured 400: `anchor-not-indexed`, `term-too-common`, …) has no tree
// and no match roots (cw-s3 dev, `?d=2610091201&f=^qwen`, 2026-10-10): the path bar's root crumb read the store's
// generic "all buckets" instead of the deployment's `ROOT_LABEL`, and the size chart "fewer than two scans hold
// this path" instead of the refusal.

const REFUSAL = JSON.stringify({ error: 'Anchored names (`^qwen`) aren’t indexed for this scan.', code: 'anchor-not-indexed' })
const crumb = (props: Omit<Parameters<typeof RootCrumb>[0], 'onClick' | 'scopeWord'>) =>
  renderToStaticMarkup(createElement(RootCrumb, { scopeWord: 'all buckets', onClick: () => {}, ...props }))
const slot = (props: Parameters<typeof SeriesEmpty>[0]) => renderToStaticMarkup(createElement(SeriesEmpty, props))
const tip = (button: string) => `<span class="tt-ref" tabindex="0">${button}</span>`

describe('RootCrumb: the root reads as the unfiltered view would', () => {
  it('a tree names its root; with none (refused), the deployment\'s label; with neither, the store\'s word', () => {
    expect([
      crumb({ treeName: 'marin CoreWeave (dev)', rootLabel: 'marin CoreWeave (dev)', here: true }),
      crumb({ rootLabel: 'marin CoreWeave (dev)', here: true }),
      crumb({ rootLabel: 'marin CoreWeave (dev)', here: false }),
      crumb({ here: true }),
    ]).toEqual([
      tip('<button type="button" class="here">marin CoreWeave (dev)</button>'),
      tip('<button type="button" class="here">marin CoreWeave (dev)</button>'),
      tip('<button type="button" class="">marin CoreWeave (dev)</button>'),
      tip('<button type="button" class="here">all buckets</button>'),
    ])
  })
})

describe('SeriesEmpty: the size chart\'s slot with no chart', () => {
  it('the page view\'s refusal, as the filter box and map state it: muted, no retry', () => {
    expect(slot({ err: apiError(400, REFUSAL), loading: false, onRetry: () => {} })).toEqual(
      '<p class="loading filter-refused" role="status">Anchored names (`^qwen`) aren’t indexed for this scan.</p>',
    )
  })
  it('any other failure in red with a retry; no failure: the fewer-than-two note', () => {
    expect([slot({ err: apiError(503, 'D1_ERROR'), loading: false, onRetry: () => {} }), slot({ loading: false })]).toEqual([
      '<p class="loading load-failed" role="alert">series failed: 503: D1_ERROR <button type="button" class="linkish">retry</button></p>',
      '<p class="loading">fewer than two scans hold this path</p>',
    ])
  })
})
