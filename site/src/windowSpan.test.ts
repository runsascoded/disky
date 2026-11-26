import { describe, expect, it } from 'vitest'

import { fmtRate, fmtSpan, rateUnit, spanUnits } from './windowSpan'

const MIN = 60_000
const H = 60 * MIN
const D = 24 * H
const signed = (d: number) => (d >= 0 ? '+' : '−') + Math.abs(d).toFixed(1)

describe('fmtSpan', () => {
  it('states a window in its two largest units, rounded at the second', () => {
    // Ryan's example: 11/19 19:03 → 11/25 13:02
    const ryans = Date.UTC(2026, 10, 25, 13, 2) - Date.UTC(2026, 10, 19, 19, 3)
    expect([45 * MIN, 3 * H + 20 * MIN, 6 * H, 47 * H + 59 * MIN, ryans, 6 * D, 6 * D + 25 * MIN].map(fmtSpan)).toEqual(
      ['45m', '3h 20m', '6h', '47h 59m', '5d 18h', '6d', '6d'],
    )
  })
})

describe('spanUnits', () => {
  it('gives the same length in hours, minutes, days and weeks', () => {
    expect(spanUnits(5 * D + 17 * H + 59 * MIN)).toEqual(['138 hours', '8,279 minutes', '5.75 days', '0.82 weeks'])
    expect(spanUnits(7 * D)).toEqual(['168 hours', '10,080 minutes', '7 days', '1 week'])
    expect(spanUnits(H)).toEqual(['1 hour', '60 minutes', '0.04 days', '0.01 weeks'])
  })
})

describe('rateUnit', () => {
  it('fits the largest of h, day, mo, yr no longer than the window', () => {
    expect([30 * MIN, 3 * H, D, 5 * D, 40 * D, 400 * D].map(rateUnit)).toEqual(['h', 'h', 'day', 'day', 'mo', 'yr'])
  })
})

describe('fmtRate', () => {
  it('divides the Δ by the window and scales it to the unit, signed as the Δ', () => {
    expect([
      fmtRate(129, 5 * D, 'day', signed),
      fmtRate(129, 5 * D, 'h', signed),
      fmtRate(-12, 2 * H, 'h', signed),
      fmtRate(0, 2 * H, 'h', signed),
    ]).toEqual(['+25.8/day', '+1.1/h', '−6.0/h', '+0.0/h'])
  })
})
