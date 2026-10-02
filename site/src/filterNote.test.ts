import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { FilterNote } from './FilterNote'

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
    ['a parse error, inline, instead of searching', { error: 'type at least 3 characters (“gr”)', matched: null },
      '<span class="fnote ferr" role="alert">type at least 3 characters (“gr”)</span>'],
    ['nothing yet', { matched: null }, ''],
  ])('%s', (_, props, want) => {
    expect(render(props)).toBe(want)
  })
})
