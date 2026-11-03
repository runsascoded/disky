/** Python's formatting where JS's differs: exact ties round to even. */
import { describe, expect, it } from 'vitest'
import { span } from './common'
import { fixed, grouped, pyRound } from './pyfmt'

describe('fixed / grouped / pyRound', () => {
  it("round an exact tie to even, as Python's format and round do", () => {
    expect([fixed(712.5, 0), fixed(713.5, 0), fixed(2.25, 1), fixed(2.35, 1), fixed(-0.25, 1)]).toEqual(['712', '714', '2.2', '2.4', '-0.2'])
    // 2.35 is not a tie in binary (2.35000000000000008882…): up, as both round it
  })
  it('round everything else to nearest, sign kept on a negative zero', () => {
    expect([fixed(0.15, 1), fixed(1.005, 2), fixed(-0.04, 1), fixed(0, 1)]).toEqual(['0.1', '1.00', '-0.0', '0.0'])
  })
  it('group thousands', () => {
    expect([grouped(1234567.5, 0), grouped(999.96, 1), grouped(-12345.25, 1), grouped(5, 0)]).toEqual(['1,234,568', '1,000.0', '-12,345.2', '5'])
  })
  it('round(x, d) to the double Python returns', () => {
    expect([pyRound(0.5), pyRound(1.5), pyRound(2.675, 2), pyRound(-3.45, 1)]).toEqual([0, 2, 2.67, -3.5])
  })
  it("a span's hours round half to even (30h30m → 1d6h)", () => {
    expect([span(0, 30.5 * 3600_000), span(0, 31.5 * 3600_000), span(0, 0)]).toEqual(['1d6h', '1d8h', '0h'])
  })
})
