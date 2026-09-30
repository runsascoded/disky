import { describe, expect, it } from 'vitest'
import { type OverTime, overTimePoint, plainRow } from './overTime.js'

describe('plainRow', () => {
  it('turns every BigInt field into a number and leaves the rest alone', () => {
    expect(plainRow({ depth: 1n, path: 'a/b', b: 12345678901234n, o: 3n, __scan_lo: 0n, __scan_hi: 15n })).toEqual(
      { depth: 1, path: 'a/b', b: 12345678901234, o: 3, __scan_lo: 0, __scan_hi: 15 },
    )
    expect(plainRow({ depth: 2, path: 'x', b: null })).toEqual({ depth: 2, path: 'x', b: null })
  })
})

const line = (covered: string[], points: Record<string, [number, number]>): OverTime => ({
  covered: new Set(covered),
  points: new Map(Object.entries(points).map(([d, [b, o]]) => [d, { b, o }])),
})

describe('overTimePoint', () => {
  const a = line(['d1', 'd2', 'd3'], { d2: [10, 1], d3: [30, 3] })
  const b = line(['d1', 'd2', 'd3'], { d3: [5, 2] })
  const short = line(['d1', 'd2'], { d2: [7, 1] })

  it('reads a covered scan from the groups: the sum, or null when every root is absent', () => {
    expect(['d1', 'd2', 'd3'].map(d => overTimePoint([a], d))).toEqual([null, { b: 10, o: 1 }, { b: 30, o: 3 }])
    expect(['d1', 'd2', 'd3'].map(d => overTimePoint([a, b], d))).toEqual([null, { b: 10, o: 1 }, { b: 35, o: 5 }])
  })

  it('defers a scan outside any line\'s groups (the unsealed tip) to the per-scan read', () => {
    expect(['d2', 'd3', 'd4'].map(d => overTimePoint([a, short], d))).toEqual([{ b: 17, o: 2 }, undefined, undefined])
    expect(overTimePoint([], 'd1')).toBe(undefined)
  })
})
