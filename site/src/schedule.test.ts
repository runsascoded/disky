import { describe, expect, it } from 'vitest'
import { nextScan } from './schedule'

const NY = 'America/New_York'
const iso = (d: Date | null) => d?.toISOString() ?? null

describe('nextScan — the next HH:MM slot in the laptop’s zone, strictly after now', () => {
  it('later today, tomorrow’s first slot past the last, and exactly at a slot means the next one', () => {
    expect([
      nextScan(['06:00', '18:00'], NY, new Date('2026-10-16T15:00:00Z')),  // 11:00 EDT
      nextScan(['06:00', '18:00'], NY, new Date('2026-10-17T01:00:00Z')),  // 21:00 EDT
      nextScan(['18:00', '06:00'], NY, new Date('2026-10-16T22:00:00Z')),  // 18:00 EDT exactly, slots unsorted
      nextScan(['06:00'], 'UTC', new Date('2026-10-16T05:59:00Z')),
    ].map(iso)).toEqual([
      '2026-10-16T22:00:00.000Z',
      '2026-10-17T10:00:00.000Z',
      '2026-10-17T10:00:00.000Z',
      '2026-10-16T06:00:00.000Z',
    ])
  })
  it('across DST: the slot keeps its wall-clock time, so its UTC instant shifts by an hour', () => {
    expect([
      nextScan(['06:00'], NY, new Date('2026-10-31T12:00:00Z')),  // → Sun Nov 1, 06:00 EST (fall back that morning)
      nextScan(['06:00'], NY, new Date('2026-03-07T12:00:00Z')),  // → Sun Mar 8, 06:00 EDT (spring forward at 02:00)
    ].map(iso)).toEqual([
      '2026-11-01T11:00:00.000Z',
      '2026-03-08T10:00:00.000Z',
    ])
  })
  it('null for no valid slot, a malformed schedule, or an unknown zone; bad slots are skipped', () => {
    const now = new Date('2026-10-16T15:00:00Z')
    expect([
      nextScan([], NY, now),
      nextScan(['6pm', '24:00', 7], NY, now),
      nextScan('06:00', NY, now),
      nextScan(['06:00'], 'Mars/Olympus', now),
      nextScan(['06:00'], undefined, now),
      nextScan(['bogus', '18:00'], NY, now),
    ].map(iso)).toEqual([null, null, null, null, null, '2026-10-16T22:00:00.000Z'])
  })
})
