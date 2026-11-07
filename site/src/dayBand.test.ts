import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { AgeChart } from './AgeChart'
import { gradientBand } from './dayBand'
import { dateAxis } from './dateScale'

it("the gradient brushes the day under the pointer ± 5% of the span, clamped to the range", () => {
  const range = dateAxis({ min: 20_000, max: 20_100 }, 'lin')
  expect([0, 0.5, 0.123, 1, -0.2, 1.4].map(t => gradientBand(range, t, 'd'))).toEqual([
    { lo: 20_000, hi: 20_005, axis: 'd' },
    { lo: 20_045, hi: 20_055, axis: 'd' },
    { lo: 20_007, hi: 20_017, axis: 'd' },
    { lo: 20_095, hi: 20_100, axis: 'd' },
    { lo: 20_000, hi: 20_005, axis: 'd' },
    { lo: 20_095, hi: 20_100, axis: 'd' },
  ])
  expect(gradientBand(dateAxis({ min: 20_000, max: 20_008 }, 'lin'), 0.5, 'a')).toEqual({ lo: 20_004, hi: 20_004, axis: 'a' })
})

it("a band from the map's gradient shades exactly the age chart's bars it overlaps", () => {
  const html = renderToStaticMarkup(createElement(AgeChart, {
    rows: [20_000, 20_001, 20_002, 20_003, 20_004].map(d => ({ d, b: 1024, o: 1, d1: 'x' })),
    catOrder: ['x'],
    mode: 'date',
    userIdx: new Map(),
    band: { lo: 20_001, hi: 20_002, axis: 'd' },
  }))
  const shaded = [...html.matchAll(/<rect x="([\d.]+)" y="0" width="([\d.]+)" height="[\d.]+" class="hoverband"/g)].map(([, x, w]) => Math.round(Number(x) / Number(w)))
  expect(shaded).toEqual([1, 2])
})
