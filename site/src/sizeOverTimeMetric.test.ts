import { describe, expect, it } from 'vitest'
import { fmtCountStep } from './SizeOverTime'

describe('fmtCountStep', () => {
  it('labels object-count ticks in K / M / G with the decimals that tell ticks `step` apart', () => {
    expect([
      fmtCountStep(94_500_000, 20_000_000),
      fmtCountStep(94_500_000, 500_000),
      fmtCountStep(94_520_000, 20_000),
      fmtCountStep(1_200, 100),
      fmtCountStep(-3_000_000, 1_000_000),
      fmtCountStep(42, 10),
      fmtCountStep(94_500_000, 0),
    ]).toEqual(['95M', '94.5M', '94.52M', '1.2K', '-3M', '42', '94.5M'])
  })
})
