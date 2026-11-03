import { describe, expect, it } from 'vitest'
import { newestLabel, shareOfTotal } from './viewNums'
import { fmtInstant } from './scan'

describe('shareOfTotal', () => {
  it('the total itself (the root, or a view equal to it) has no share line', () => {
    expect([shareOfTotal(2992, 2992), shareOfTotal(0, 0)]).toEqual([null, null])
  })
  it('a part prints its share to two places', () => {
    expect([shareOfTotal(1, 4), shareOfTotal(1, 3)]).toEqual(['25.00% of total', '33.33% of total'])
  })
  it('a part that would round to the whole says it is just under it', () => {
    expect(shareOfTotal(999_999, 1_000_000)).toBe('>99.99% of total')
  })
})

describe('newestLabel', () => {
  // Local wall-clock instants, so the labels hold in any timezone the suite runs in.
  const now = new Date(2026, 9, 21, 18, 0)
  const at = (y: number, mo: number, d: number, h: number, mi: number) => new Date(y, mo - 1, d, h, mi).getTime() / 1000

  it('an instant reads as the scan picker labels a scan: local, 12-hour, bare a/p', () => {
    expect([
      newestLabel({ d: 20747, t: at(2026, 10, 21, 10, 39) }, now),
      newestLabel({ d: 20747, t: at(2026, 10, 21, 0, 5) }, now),
      newestLabel({ d: 20747, t: at(2026, 10, 21, 13, 0) }, now),
      newestLabel({ d: 20382, t: at(2025, 10, 21, 22, 7) }, now),
    ]).toEqual(['10/21 10:39a', '10/21 12:05a', '10/21 1:00p', '2025-10-21 10:07p'])
  })
  it('an answer cached before `t` keeps its date alone; no date, no label', () => {
    expect([newestLabel({ d: Date.UTC(2026, 9, 21) / 86_400_000 }, now), newestLabel({}, now)]).toEqual(['10/21', null])
  })
  it('the scan picker’s label and this one are the same format', () => {
    expect(fmtInstant(new Date(2026, 9, 9, 0, 30).getTime(), now)).toBe('10/9 12:30a')
  })
})
