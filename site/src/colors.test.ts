import { describe, expect, it } from 'vitest'
import { epochDaysToDate } from './colors'

describe('epochDaysToDate', () => {
  it('month/day in the current year, month/day/yy otherwise', () => {
    const now = new Date('2026-10-16T12:00:00Z')
    const day = (iso: string) => Date.parse(iso) / 86400_000
    expect([day('2026-08-21'), day('2025-08-21'), 0].map(d => epochDaysToDate(d, now))).toEqual(['8/21', '8/21/25', '1/1/70'])
  })
})
