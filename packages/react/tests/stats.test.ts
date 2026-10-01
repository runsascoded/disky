import { describe, expect, it } from 'vitest'
import { pow10 } from '../src/stats'

describe('pow10', () => {
  // `10 ** -4` is 0.0001 on some engines and 0.00009999999999999999 on others
  // (macOS dev vs Linux CI) — decade ticks must not depend on which.
  it('is bit-exact against decimal literals for negative exponents', () => {
    expect(pow10(-1)).toBe(0.1)
    expect(pow10(-2)).toBe(0.01)
    expect(pow10(-3)).toBe(0.001)
    expect(pow10(-4)).toBe(0.0001)
    expect(pow10(-5)).toBe(0.00001)
    expect(pow10(-6)).toBe(0.000001)
    expect(pow10(-7)).toBe(1e-7)
    expect(pow10(-11)).toBe(1e-11)
  })

  it('is exact for zero and positive exponents', () => {
    expect(pow10(0)).toBe(1)
    expect(pow10(1)).toBe(10)
    expect(pow10(6)).toBe(1_000_000)
    expect(pow10(12)).toBe(1e12)
    expect(pow10(22)).toBe(1e22)
  })
})
