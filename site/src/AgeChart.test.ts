import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { AgeChart } from './AgeChart'
import type { ColorMode } from './types'

const render = (modes: ColorMode[], onMode?: (mode: ColorMode) => void) =>
  renderToStaticMarkup(createElement(AgeChart, {
    rows: [{ d: 20_000, b: 1024, o: 1, d1: 'checkpoints' }],
    catOrder: ['checkpoints'],
    mode: 'date',
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

  it('does not offer an override without a change handler', () => {
    expect(controls(render(['date', 'tree']))).toEqual([granularity])
  })
})
