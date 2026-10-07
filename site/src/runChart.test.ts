import { describe, expect, it } from 'vitest'
import { alignedTicks, axisTicks, chartRange, durationLabel, parseDuration, percentOf, rollingRates, runWindowParam, shiftWindow, zoomWindow } from './runChart'
import type { HistoryPoint } from './runHistory'

describe('run time range and axes', () => {
  it('uses URL-safe end + duration, with latest and all-time modes', () => {
    const fixed = { end: Date.parse('2026-10-05T12:00:00Z') / 1000, duration: 5400 }
    expect([runWindowParam.encode(fixed), runWindowParam.decode(runWindowParam.encode(fixed)), runWindowParam.decode('all'), runWindowParam.decode('2h'), runWindowParam.decode('bad~1h')]).toEqual([
      '2026-10-05T12:00:00.000Z~90m', fixed, { end: null, duration: null }, { end: null, duration: 7200 }, { end: null, duration: 3600 },
    ])
  })
  it('accepts continuous compound durations but rejects partial/zero/unbounded input', () => {
    expect(['1h30m', '1.5h', '90m', '0s', 'hi1h', '1hgarbage', '400d'].map(parseDuration)).toEqual([5400, 5400, 5400, null, null, null, null])
    expect([60, 3600, 5400, 86400].map(durationLabel)).toEqual(['1m', '1h', '90m', '1d'])
  })
  it('keeps a live range anchored to the clock even if samples are stale', () => {
    const points: HistoryPoint[] = [{ ts: 1000, objects: 10, bytes: 100, rate: 1, byteRate: 10 }]
    expect([chartRange(points, { end: null, duration: 60 }, 2000), chartRange(points, { end: 1500, duration: null }, 2000), shiftWindow([1000, 1600], -1), zoomWindow([1000, 1600], 0.5)]).toEqual([
      [1940, 2000], [1000, 1500], { end: 1300, duration: 600 }, { end: 1450, duration: 300 },
    ])
  })
  it('makes readable zero-based y ticks and preserves percentages over 100', () => {
    expect([axisTicks(5952), axisTicks(151687316), axisTicks(0)]).toEqual([[0, 2000, 4000, 6000], [0, 50000000, 100000000, 150000000, 200000000], [0, 0.25, 0.5, 0.75, 1]])
    expect([percentOf(3, 4), percentOf(5, 4), percentOf(0, 0)]).toEqual(['75.0%', '125.0%', '—'])
    expect([percentOf(2999, 3000), percentOf(1, 10000), percentOf(3000, 3000)]).toEqual(['>99.9%', '<0.1%', '100.0%'])
  })
  it('weights rolling rates by time and breaks smoothing at missing intervals', () => {
    const points = [
      { ts: 0, objects: 0, bytes: 0, rate: null, byteRate: null },
      { ts: 30, objects: 300, bytes: 300, rate: 10, byteRate: 100 },
      { ts: 90, objects: 1500, bytes: 1500, rate: 20, byteRate: 200 },
      { ts: 500, objects: 2000, bytes: 2000, rate: null, byteRate: null },
      { ts: 530, objects: 2900, bytes: 2900, rate: 30, byteRate: 300 },
    ]
    expect(rollingRates(points, 60)).toEqual([null, 10, 20, null, 30])
    expect(rollingRates(points, 90)).toEqual([null, 10, 50 / 3, null, 30])
    expect(rollingRates(points, 90, 'byteRate')).toEqual([null, 100, 500 / 3, null, 300])
  })
  it('aligns dual axes at nice shared ticks in display units', () => {
    expect(alignedTicks(175466921, 50.4)).toEqual([[0, 50000000, 100000000, 150000000, 200000000], [0, 20, 40, 60, 80]])
    expect(alignedTicks(5952, 50.4)).toEqual([[0, 2000, 4000, 6000], [0, 20, 40, 60]])
  })
})
