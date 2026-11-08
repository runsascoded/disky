import { describe, expect, it } from 'vitest'
import { durationAxis, durationSegments } from './durationAxis'

describe('duration axis breaks', () => {
  it('keeps ordinary durations on a zero-based linear axis', () => {
    expect(durationAxis([0, 0.2, 0.5, 1, 1.2, 1.5, 2])).toEqual({ ticks: [0, 0.5, 1, 1.5, 2], top: 2, cut: null })
    expect(durationAxis([0, NaN, Infinity])).toEqual({ ticks: [0, 0.25, 0.5, 0.75, 1], top: 1, cut: null })
  })
  it('collapses only a large empty gap, retaining endpoints and a visible outlier cap', () => {
    expect(durationAxis([0, 0.1, 0.25, 1, 2, 102, 104])).toEqual({
      ticks: [0, 0.5, 1, 1.5, 2, 101.5, 102.5, 103.5, 104.5], top: 104.5, cut: [2, 101.5],
    })
    expect(durationAxis([0, 0.1, 1, 15, 96, 98])).toEqual({ ticks: [0, 5, 10, 15, 95, 96, 97, 98], top: 98, cut: [15, 95] })
  })
  it('does not omit a phase boundary in the apparent gap, and supports a full-scale override', () => {
    const full = { ticks: [0, 50, 100, 150], top: 150, cut: null }
    expect(durationAxis([1, 2, 102, 104], true, [0, 40])).toEqual(full)
    expect(durationAxis([0, 1, 2, 102, 104], false)).toEqual(full)
    expect(durationAxis([1, 1, 1], true, [0, 0.1, 1 / 6])).toEqual({ ticks: [0, 0.5, 1], top: 1, cut: null })
  })
  it('splits whole bars and stacked phases without painting across the break', () => {
    const cut = [2, 101.5] as const
    expect([
      durationSegments(0, 104, cut), durationSegments(1, 1.5, cut), durationSegments(3, 100, cut),
      durationSegments(100, 104, cut), durationSegments(1, 3, cut), durationSegments(2, 2, cut), durationSegments(0, 104, null),
    ]).toEqual([[[0, 2], [101.5, 104]], [[1, 1.5]], [], [[101.5, 104]], [[1, 2]], [], [[0, 104]]])
  })
})
