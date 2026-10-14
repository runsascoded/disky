import { describe, expect, it } from 'vitest'
import { fmtRunTime, runDay } from './scanRunsUi'

describe('scan-run display times', () => {
  const at = Date.parse('2026-10-13T02:06:00Z') / 1000
  it('uses the viewer timezone for the timestamp and chart day across midnight', () => {
    expect([
      fmtRunTime(at, 'America/New_York'), runDay(at, 'America/New_York'),
      fmtRunTime(at, 'Asia/Tokyo'), runDay(at, 'Asia/Tokyo'),
      fmtRunTime(null),
    ]).toEqual([
      'Oct 12, 2026, 10:06 PM EDT', '2026-10-12',
      'Oct 13, 2026, 11:06 AM GMT+9', '2026-10-13',
      '—',
    ])
  })
})
