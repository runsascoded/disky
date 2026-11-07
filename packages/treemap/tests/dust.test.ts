import { describe, expect, it } from 'vitest'
import { dustLineCount, dustOffsets } from '../src/DustHatch'

describe('dustOffsets', () => {
  it('places rules whose gaps grow geometrically from the dense end', () => {
    // n=4, ratio 2: gaps 6.67, 13.33, 26.67, 53.33 (Σ = 100). The final rule
    // lands on the far edge and is dropped (it's the border), leaving 3
    // interior offsets from the dense end.
    const off = dustOffsets(100, 4, 2)
    expect(off.map(x => Math.round(x))).toEqual([7, 20, 47])
    // The step between consecutive offsets grows (sparser away from the dense end).
    const gaps = off.map((x, i) => x - (off[i - 1] ?? 0))
    expect(gaps.every((g, i) => i === 0 || g > gaps[i - 1])).toBe(true)
  })

  it('spaces evenly when ratio is 1', () => {
    // Even 25px gaps; the rule at the far edge (100) is dropped.
    expect(dustOffsets(100, 4, 1).map(x => Math.round(x))).toEqual([25, 50, 75])
  })

  it('returns nothing for a non-positive length or count', () => {
    expect(dustOffsets(0, 4, 1.3)).toEqual([])
    expect(dustOffsets(100, 0, 1.3)).toEqual([])
  })
})

describe('dustLineCount', () => {
  it('grows with log2(count) but never packs rules under ~4px apart', () => {
    // base·log2(count+1): count 1 → base·1, count 7 → base·3, count 63 → base·6.
    expect(dustLineCount(400, 1, 3)).toBe(3)
    expect(dustLineCount(400, 7, 3)).toBe(9)
    expect(dustLineCount(400, 63, 3)).toBe(18)
    // Never fewer than 2.
    expect(dustLineCount(400, 0, 3)).toBe(2)
    // Clamped by len/4: a 20px axis holds at most 5 rules however dense.
    expect(dustLineCount(20, 1_000_000, 3)).toBe(5)
  })
})
