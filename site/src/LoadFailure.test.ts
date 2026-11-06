import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { apiError } from './filterCaps'
import { LevelFailure, LoadFailure, mapSlot } from './LoadFailure'

// A failed map, diff or series load, stated inline (`LoadFailure`), and the map slot's precedence (`mapSlot`): the
// gcs incident (2026-10-09) was a 500 on every heavy literal, drawn as a held gray map under "loading view…" for
// 80 s and more, the error never shown.

const INCIDENT = 'subtree failed: static names: static-names/2026-10-08c/deltas/2026-10-09_2026-10-09T1236/catalog/meta.json is missing'
const render = (err: unknown, what = 'view', as?: 'p' | 'span', className?: string) =>
  renderToStaticMarkup(createElement(LoadFailure, { err, what, onRetry: () => {}, ...(as ? { as } : {}), ...(className ? { className } : {}) }))
const RETRY = '<button type="button" class="linkish">retry</button>'

describe('LoadFailure', () => {
  it('the incident\'s 500: what failed, the server\'s message (its first 120 characters), a retry', () => {
    expect(render(apiError(500, INCIDENT))).toEqual(
      `<p class="loading load-failed" role="alert">view failed: 500: ${INCIDENT.slice(0, 120)} ${RETRY}</p>`,
    )
  })

  it('a retry for a 5xx or no response at all; none for a 4xx (the same request fails the same way)', () => {
    expect([
      render(apiError(503, 'index backend unavailable, retry: D1_ERROR')),
      render(new TypeError('Failed to fetch')),
      render(apiError(404, 'no such scan')),
      render(apiError(400, 'bad split (want roots)')),
    ]).toEqual([
      `<p class="loading load-failed" role="alert">view failed: 503: index backend unavailable, retry: D1_ERROR ${RETRY}</p>`,
      `<p class="loading load-failed" role="alert">view failed: Failed to fetch ${RETRY}</p>`,
      '<p class="loading load-failed" role="alert">view failed: 404: no such scan</p>',
      '<p class="loading load-failed" role="alert">view failed: 400: bad split (want roots)</p>',
    ])
  })

  it('a structured refusal keeps its friendly reason: no status, no retry', () => {
    const notIndexed = JSON.stringify({ error: 'This scan isn’t indexed for name search yet.', code: 'scan-not-indexed' })
    expect([render(apiError(400, notIndexed)), render(apiError(400, notIndexed), 'diff 10/8 → 10/9', 'span', 'tab-note')]).toEqual([
      '<p class="loading filter-refused" role="status">This scan isn’t indexed for name search yet.</p>',
      '<span class="tab-note filter-refused" role="status">This scan isn’t indexed for name search yet.</span>',
    ])
  })

  it('the diff\'s and the series\' 500s, in their lines', () => {
    expect([render(apiError(500, 'diff failed: boom'), 'diff 10/8 → 10/9', 'span', 'tab-note'), render(apiError(500, 'series failed: boom'), 'series', 'p', 'sub')]).toEqual([
      `<span class="tab-note load-failed" role="alert">diff 10/8 → 10/9 failed: 500: diff failed: boom ${RETRY}</span>`,
      `<p class="sub load-failed" role="alert">series failed: 500: series failed: boom ${RETRY}</p>`,
    ])
  })
})

describe('mapSlot: the asked-for view\'s failure outranks a held tree', () => {
  it('tree → map; failed → failed (held or not); loading → the held tree, else the skeleton', () => {
    const tree = { n: 'root' }, held = { n: 'previous' }, err = apiError(500, INCIDENT)
    expect([
      mapSlot(tree, held, null), mapSlot(tree, held, err),
      mapSlot(null, held, err), mapSlot(null, null, err),
      mapSlot(null, held, null), mapSlot(null, null, null),
    ]).toEqual(['map', 'map', 'failed', 'failed', 'held', 'skeleton'])
  })
})

describe('LevelFailure: a level of the drilled path that didn\'t load', () => {
  const level = (err: unknown, shown: string | null = 'bk/a') =>
    renderToStaticMarkup(createElement(LevelFailure, { path: 'bk/a/b', err, onRetry: () => {}, ...(shown ? { shown } : {}) }))

  it('a 5xx or no response: which level, the server\'s message, what the map shows instead, a retry', () => {
    expect([level(apiError(500, 'subtree failed: boom')), level(new TypeError('Failed to fetch'))]).toEqual([
      `<p class="level-failed load-failed" role="alert">couldn’t load <code>bk/a/b</code>: 500: subtree failed: boom · showing <code>bk/a</code> ${RETRY}</p>`,
      `<p class="level-failed load-failed" role="alert">couldn’t load <code>bk/a/b</code>: Failed to fetch · showing <code>bk/a</code> ${RETRY}</p>`,
    ])
  })

  it('a path the scan doesn\'t hold (404) reads as that: no retry, the same request answers the same', () => {
    expect(level(apiError(404, 'path not found'))).toEqual(
      '<p class="level-failed" role="status"><code>bk/a/b</code> isn’t in this scan · showing <code>bk/a</code></p>',
    )
  })

  it('any other 4xx keeps its message and offers no retry; a filter refusal keeps its reason', () => {
    const refused = JSON.stringify({ error: 'This term is too common to search below the root.', code: 'term-too-common' })
    expect([level(apiError(413, 'view too wide')), level(apiError(400, refused))]).toEqual([
      '<p class="level-failed load-failed" role="alert">couldn’t load <code>bk/a/b</code>: 413: view too wide · showing <code>bk/a</code></p>',
      '<p class="level-failed filter-refused" role="status"><code>bk/a/b</code>: This term is too common to search below the root. · showing <code>bk/a</code></p>',
    ])
  })

  it('in the map\'s place (all it has is the failed level\'s own node, empty): nothing is named as shown', () => {
    expect(level(apiError(503, 'index backend unavailable'), null)).toEqual(
      `<p class="level-failed load-failed" role="alert">couldn’t load <code>bk/a/b</code>: 503: index backend unavailable ${RETRY}</p>`,
    )
  })
})
