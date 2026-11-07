import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { AgeChart } from './AgeChart'
import type { ColorMode } from './types'

const render = (modes: ColorMode[], onMode?: (mode: ColorMode) => void, mapMode: ColorMode = 'date') =>
  renderToStaticMarkup(createElement(AgeChart, {
    rows: [{ d: 20_000, b: 1024, o: 1, d1: 'checkpoints' }],
    catOrder: ['checkpoints'],
    mode: 'date',
    mapMode,
    modes,
    onMode,
    userIdx: new Map(),
  }))

/** The radio groups and their complete ordered choices, including selection. */
const controls = (html: string) =>
  [...html.matchAll(/<span class="gran" role="radiogroup" aria-label="([^"]+)">((?:<span[^>]*>[^<]*<\/span>|<button[^>]*>[^<]*<\/button>)*)<\/span>/g)].map(([, label, body]) => ({
    label,
    choices: [...body.matchAll(/<button role="radio" aria-checked="(true|false)" class="[^"]*">([^<]*)<\/button>/g)].map(([, checked, text]) => ({
      label: text,
      selected: checked === 'true',
    })),
  }))

const granularity = {
  label: 'Time granularity',
  choices: [
    { label: 'month', selected: false },
    { label: 'week', selected: false },
    { label: 'day', selected: true },
  ],
}

describe('age chart color override', () => {
  it.each(['gs://', 'r2://', 's3://'])('names the actual listing timestamp for %s', scheme => {
    const html = renderToStaticMarkup(createElement(AgeChart, {
      rows: [{ d: 20_000, b: 1024, o: 1 }],
      catOrder: [], mode: 'date', userIdx: new Map(), scheme,
    }))
    expect([...html.matchAll(/<svg[^>]*role="img" aria-label="([^"]+)"/g)].map(([, label]) => label)).toEqual([
      scheme === 'gs://' ? 'Bytes by created day' : 'Bytes by modified day',
    ])
  })
  it.each<{ modes: ColorMode[] }>([{ modes: [] }, { modes: ['date'] }])('hides the picker without multiple available axes: $modes', ({ modes }) => {
    expect(controls(render(modes, () => {}))).toEqual([granularity])
  })

  it('offers every available axis when there is a choice', () => {
    expect(controls(render(['date', 'tree'], () => {}))).toEqual([
      {
        label: 'Color by',
        choices: [
          { label: 'written', selected: true },
          { label: 'tree', selected: false },
        ],
      },
      granularity,
    ])
  })

  it('selects no axis while the bars are neutral (the map\'s axis is one the strata lack)', () => {
    expect(controls(render(['date', 'read'], () => {}, 'tree'))[0]).toEqual({
      label: 'Color by',
      choices: [
        { label: 'written', selected: false },
        { label: 'read', selected: false },
      ],
    })
  })

  it('does not offer an override without a change handler', () => {
    expect(controls(render(['date', 'tree']))).toEqual([granularity])
  })
})

/** The snapshot bars' fills, the gradient legend's presence, and the value labels' text + placement. */
const chart = (html: string) => ({
  fills: [...html.matchAll(/<rect x="[^"]+" y="[^"]+" width="[^"]+" height="[^"]+" rx="[^"]+" fill="([^"]+)"/g)].map(([, f]) => f),
  gradient: [...html.matchAll(/<span class="li gradli">([a-z]+)/g)].map(([, t]) => t),
  labels: [...html.matchAll(/<text [^>]*class="val ([a-z]+)">([^<]*)<\/text>/g)].map(([, pos, text]) => ({ pos, text })),
})

describe('age chart bars', () => {
  const rows = [
    { d: 20_000, b: 52e9, o: 1 },
    { d: 20_040, b: 3e9, o: 1 },
    { d: 20_070, b: 0.2e9, o: 1 },
  ]
  const render = (mapMode: ColorMode, picked = false) => renderToStaticMarkup(createElement(AgeChart, {
    rows, catOrder: [], mode: 'date', mapMode, picked, userIdx: new Map(),
  }))

  it('neutral under a non-date map lens: one theme color, no gradient legend; wide bars labeled', () => {
    expect(chart(render('tree'))).toEqual({
      fills: ['var(--age-bar)', 'var(--age-bar)', 'var(--age-bar)'],
      gradient: [],
      labels: [
        { pos: 'inside', text: '52 G' },
        { pos: 'above', text: '3.0 G' },
        { pos: 'above', text: '200 M' },
      ],
    })
  })

  it('the date gradient under the map\'s date lens, with its legend', () => {
    expect(chart(render('date'))).toEqual({
      fills: ['rgb(68,1,84)', 'rgb(33,145,140)', 'rgb(253,231,37)'],
      gradient: ['older'],
      labels: [
        { pos: 'inside', text: '52 G' },
        { pos: 'above', text: '3.0 G' },
        { pos: 'above', text: '200 M' },
      ],
    })
  })

  it('the date gradient when picked in the chart, whatever the map shows', () => {
    expect(chart(render('user', true)).gradient).toEqual(['older'])
  })
})
