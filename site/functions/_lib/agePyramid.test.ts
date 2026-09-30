import { describe, expect, it } from 'vitest'
import { planAge, variantForPlan } from './agePyramid'

describe('planAge — budget picks the finest tier that fits', () => {
  // A 30-day window. Bin counts: 1h=720, 3h=240, 6h=120, 12h=60, 1d=30,
  // 2d=15, 4d=8, 8d=4, 16d=2. Budgets chosen with margin off the boundaries.
  const FROM = new Date('2026-06-01T00:00:00Z')
  const TO = new Date('2026-07-01T00:00:00Z')
  const cases: [number, string][] = [
    [1000, 'age-pyramid-1h'],  // 1h (720) fits
    [150, 'age-pyramid-6h'],   // 3h (240) too many, 6h (120) fits
    [40, 'age-pyramid-1d'],    // 12h (60) too many, 1d (30) fits
    [5, 'age-pyramid-8d'],     // 4d (8) too many, 8d (4) fits
  ]
  for (const [budget, variant] of cases) {
    it(`30d, budget ${budget} → ${variant}`, () => {
      expect(variantForPlan(planAge(FROM, TO, budget))).toBe(variant)
    })
  }

  it('a 6-year window under budget 3 falls back to the coarsest tier (8d)', () => {
    const from = new Date('2020-01-01T00:00:00Z')
    const to = new Date('2026-01-01T00:00:00Z')
    // 8d ≈ 274 bins over 6yr — still > 3, so the coarsest is chosen.
    expect(variantForPlan(planAge(from, to, 3))).toBe('age-pyramid-8d')
  })
})
