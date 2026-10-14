import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { FilterNote, matchedNote } from './FilterNote'
import { apiError, apiErrorMessage, refusalOf } from './filterCaps'

const render = (props: Parameters<typeof FilterNote>[0]) => renderToStaticMarkup(createElement(FilterNote, props))
const NO_INDEX = 'this scan has no search index; small matches may be missing'

describe('the filter note: errors and completeness, spelled out', () => {
  it.each<[string, Parameters<typeof FilterNote>[0], string]>([
    ['complete', { matched: '7 MiB matched', coverage: {} }, '<span class="fnote">7 MiB matched</span>'],
    ['partial', { matched: '7 MiB matched', coverage: { partialReason: 'the row read hit its budget (128 path groups)' } },
      '<span class="fnote">7 MiB matched<span class="fflag">partial results: the row read hit its budget (128 path groups)</span></span>'],
    ['approximate', { matched: 'no matches', coverage: { approximateReason: NO_INDEX } },
      `<span class="fnote">no matches<span class="fflag">approximate: ${NO_INDEX}</span></span>`],
    ['both', { matched: '1 KiB matched', coverage: { partialReason: 'x', approximateReason: 'y' } },
      '<span class="fnote">1 KiB matched<span class="fflag">partial results: x</span><span class="fflag">approximate: y</span></span>'],
    ['the hex-run rule applied: an info icon carrying the note', { matched: '40 B matched', coverage: { hexRuns: { min: 16, tail: 8 } } },
      '<span class="fnote">40 B matched<span class="tt-ref" tabindex="0"><span class="fflag fhex" role="note" aria-label="Matches inside long hex IDs (16+ hex digits) aren&#x27;t indexed.">ⓘ</span></span></span>'],
        ['a parse error, inline, instead of searching', { error: 'type at least 3 characters (“gr”)', matched: null },
      '<span class="fnote ferr" role="alert">type at least 3 characters (“gr”)</span>'],
    ['a heavy literal\'s fleet root from the catalog alone', { matched: '1 MiB matched fleet-wide', coverage: { bucketsOnly: true } },
      '<span class="fnote">1 MiB matched fleet-wide<span class="fflag">per-bucket totals only: searching inside a bucket for this term needs the full search index</span></span>'],
    ['nothing yet', { matched: null }, ''],
  ])('%s', (_, props, want) => {
    expect(render(props)).toBe(want)
  })
})

describe('matchedNote: the drilled folder\'s matched bytes, saying which', () => {
  it('in this folder when drilled, fleet-wide at the root', () => {
    const fmt = (b: number) => `${b} B`
    expect([matchedNote(7, true, fmt), matchedNote(7, false, fmt), matchedNote(0, true, fmt), matchedNote(0, false, fmt)])
      .toEqual(['7 B matched in this folder', '7 B matched fleet-wide', 'no matches in this folder', 'no matches'])
  })
})

describe('apiErrorMessage: a structured refusal becomes the filter box\'s message', () => {
  it('a 400 `{ error, code }` → `bad query: …`; anything else → status and text', () => {
    expect([
      apiErrorMessage(400, JSON.stringify({ error: 'Search isn’t available for this scan yet.', code: 'scan-not-indexed' })),
      apiErrorMessage(400, 'bad query: invalid regex'),
      apiErrorMessage(503, JSON.stringify({ error: 'busy' })),
    ]).toEqual(['400: bad query: Search isn’t available for this scan yet.', '400: bad query: invalid regex', '503: {"error":"busy"}'])
  })
})

describe('apiError / refusalOf: a filter refusal is a reason to state inline, not a failure', () => {
  const body = (code: string, error = `why ${code}`) => JSON.stringify({ error, code })
  it('every refusal code carries its reason; anything else is a plain failure', () => {
    expect([
      ...['unsupported-regex', 'unsupported-glob', 'unsupported-exclusion', 'unsupported-terms', 'unsupported-slash', 'unsupported-scope', 'scan-not-indexed', 'term-too-common', 'anchor-not-indexed']
        .map(code => refusalOf(apiError(400, body(code)))),
      refusalOf(apiError(400, body('invalid-regex'))),
      refusalOf(apiError(404, body('scan-not-indexed'))),
      refusalOf(apiError(400, 'bad query: invalid regex')),
      refusalOf(new Error('400: bad query: x')),
      refusalOf(undefined),
    ]).toEqual([
      ...['unsupported-regex', 'unsupported-glob', 'unsupported-exclusion', 'unsupported-terms', 'unsupported-slash', 'unsupported-scope', 'scan-not-indexed', 'term-too-common', 'anchor-not-indexed']
        .map(code => ({ code, reason: `why ${code}` })),
      null, null, null, null, null,
    ])
  })
  it('keeps the message the filter box reads', () => {
    expect([apiError(400, body('scan-not-indexed', 'Search isn’t available for this scan yet.')).message, apiError(503, 'busy').message])
      .toEqual(['400: bad query: Search isn’t available for this scan yet.', '503: busy'])
  })
})
