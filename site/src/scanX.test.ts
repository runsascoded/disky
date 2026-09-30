import { describe, expect, it } from 'vitest'
import { dateOfX, xOfScan } from './SizeOverTime'

describe('scan id ↔ chart x', () => {
  it('a date-only id is UTC midnight; a sub-daily id keeps its time', () => {
    expect(xOfScan('2026-09-17')).toBe(Date.UTC(2026, 8, 17))
    expect(xOfScan('2026-09-17T1201')).toBe(Date.UTC(2026, 8, 17, 12, 1))
    expect(xOfScan('2026-09-17T0001')).not.toBe(xOfScan('2026-09-17T1201'))
  })
  it('round-trips through dateOfX', () => {
    for (const id of ['2026-09-17', '2026-09-17T1201', '2026-09-17T0001']) expect(dateOfX(xOfScan(id))).toBe(id)
  })
})
