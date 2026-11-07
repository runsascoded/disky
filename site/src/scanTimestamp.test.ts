import { describe, expect, it } from 'vitest'
import { fmtTimestamp } from './scan'

describe('capture timestamps in the viewer timezone', () => {
  it('shows the capture in Eastern daylight time', () => {
    expect(fmtTimestamp('2026-10-12T10:39:51Z', 'America/New_York')).toBe('Oct 12, 2026, 6:39 AM EDT')
  })
  it('uses standard time in winter', () => {
    expect(fmtTimestamp('2026-12-12T10:39:51Z', 'America/New_York')).toBe('Dec 12, 2026, 5:39 AM EST')
  })
  it('shifts the calendar date in another viewer timezone', () => {
    expect(fmtTimestamp('2026-10-12T01:39:51Z', 'America/Los_Angeles')).toBe('Oct 11, 2026, 6:39 PM PDT')
    expect(fmtTimestamp('2026-10-12T01:39:51Z', 'Asia/Tokyo')).toBe('Oct 12, 2026, 10:39 AM GMT+9')
  })
})
