// Labels render in the viewer's local time: pin one (EDT on 10/9) for exact labels. Vitest runs each file in its own
// fork, so this reaches only this file.
process.env.TZ = 'America/New_York'

import { describe, expect, it } from 'vitest'
import { scanGroups, scanLabeler, scanMiss } from './scan'
import { hrefWithScan } from './NoScanMatch'
import { decodeSel, encodeSel, exactPrefix, exactSlug, nearestScan, resolveAfter, resolveBefore, resolveScan, scanInstant, scanMatches, scanNeighbors, sortScans, startKey, timesNeeded, type ScanTimes } from './scanSlug'

// gcs on 10/9: the date-only daily run, which started 04:30:12Z (its listings' earliest `started`, `meta.started`),
// then 12:36Z (8:36a EDT); a date-only 10/8 alone on its day.
const SCANS = ['2026-10-08', '2026-10-09', '2026-10-09T1236']
const NOW = new Date(Date.UTC(2026, 9, 9, 16))
const TIMES: ScanTimes = { '2026-10-09': startKey('2026-10-09', '2026-10-09T04:30:12.345Z')! }

describe('a date-only scan keyed by its real start', () => {
  it('only a date-only scan sharing its day needs a start; the start is its UTC minute', () => {
    expect([timesNeeded(SCANS), TIMES]).toEqual([['2026-10-09'], { '2026-10-09': '2026-10-09T0430' }])
  })
  it('slug, label and order: every row of a two-scan day is a time, in time order', () => {
    const label = scanLabeler(SCANS, TIMES, NOW)
    expect(sortScans(SCANS, TIMES).map(id => [id, exactSlug(id, TIMES), label(id)])).toEqual([
      ['2026-10-09T1236', '2610091236', '10/9 8:36a'],
      ['2026-10-09', '2610090430', '10/9 12:30a'],
      ['2026-10-08', '2610080000', '10/8'],
    ])
    expect(scanGroups(sortScans(SCANS, TIMES), NOW, label)).toEqual([
      { day: '10/9', scans: [{ id: '2026-10-09T1236', label: '8:36a' }, { id: '2026-10-09', label: '12:30a' }] },
      { day: '10/8', scans: [{ id: '2026-10-08', label: '10/8' }] },
    ])
  })
  it('every scan round-trips through its exact slug; the hour and minute slugs take it, the midnight stays an alias', () => {
    expect(SCANS.map(id => resolveScan(exactSlug(id, TIMES), SCANS, NOW, TIMES))).toEqual(SCANS)
    expect(SCANS.map(id => resolveAfter(decodeSel(encodeSel({ d: exactPrefix(id, TIMES) })), SCANS, TIMES))).toEqual(SCANS)
    expect(['261009', '26100904', '2610090430', '2610090000', '26100900', '26100912', '2610090431', '26100905'].map(s => resolveScan(s, SCANS, NOW, TIMES)))
      .toEqual(['2026-10-09T1236', '2026-10-09', '2026-10-09', '2026-10-09', '2026-10-09', '2026-10-09T1236', null, null])
  })
  it('the day slug is the day\'s latest scan by time, even when the date-only run started last', () => {
    const scans = ['2026-10-09T0300', '2026-10-09']
    expect([resolveScan('261009', scans, NOW, TIMES), resolveScan('261009', scans, NOW), scanMatches('2026-10-09', scans, TIMES), sortScans(scans, TIMES)]).toEqual([
      '2026-10-09', '2026-10-09T0300', ['2026-10-09', '2026-10-09T0300'], ['2026-10-09', '2026-10-09T0300'],
    ])
  })
  it('the disambiguation strip lists the day\'s scans newest first, by time', () => {
    expect(scanMatches(decodeSel('261009')!.d, SCANS, TIMES)).toEqual(['2026-10-09T1236', '2026-10-09'])
  })
  it('the nearest-scan links of a miss are keyed by the real time', () => {
    const near = (slug: string) => scanMiss(decodeSel(slug), SCANS, true, TIMES)
    expect([near('26100903'), near('26100905'), near('2610090431')]).toEqual([
      { slug: '26100903', before: '2026-10-08', after: '2026-10-09' },
      { slug: '26100905', before: '2026-10-09', after: '2026-10-09T1236' },
      { slug: '2610090431', before: '2026-10-09', after: '2026-10-09T1236' },
    ])
    expect([
      hrefWithScan('/', '?d=26100905', decodeSel('26100905'), '2026-10-09', true, TIMES),
      hrefWithScan('/', '?d=2610091236-26100903', decodeSel('2610091236-26100903'), '2026-10-09', false, TIMES),
    ]).toEqual(['/?d=2610090430', '/?d=2610091236-2610090430'])
  })
  it('a span and a pinned start resolve by the real time', () => {
    const after = '2026-10-09T1236'
    expect([
      resolveBefore({ span: 8 * 3600_000 }, after, SCANS, TIMES),
      resolveBefore({ span: 8 * 3600_000 }, after, SCANS),
      resolveBefore({ from: '2026-10-09T04' }, after, SCANS, TIMES),
      nearestScan(SCANS, Date.UTC(2026, 9, 9, 4), TIMES),
      scanInstant('2026-10-09', TIMES) - scanInstant('2026-10-09'),
    ]).toEqual(['2026-10-09', '2026-10-09', '2026-10-09', '2026-10-09', (4 * 60 + 30) * 60_000])
  })
})

describe('an unknown start falls back to the midnight form', () => {
  it('slug, label and order', () => {
    const label = scanLabeler(SCANS, {}, NOW)
    expect(sortScans(SCANS, {}).map(id => [id, exactSlug(id, {}), label(id)])).toEqual([
      ['2026-10-09T1236', '2610091236', '10/9 8:36a'],
      ['2026-10-09', '2610090000', '10/9 (time unknown)'],
      ['2026-10-08', '2610080000', '10/8'],
    ])
    expect(scanGroups(sortScans(SCANS, {}), NOW, label)).toEqual([
      { day: '10/9', scans: [{ id: '2026-10-09T1236', label: '8:36a' }, { id: '2026-10-09', label: '(time unknown)' }] },
      { day: '10/8', scans: [{ id: '2026-10-08', label: '10/8' }] },
    ])
  })
  it('resolves by midnight', () => {
    expect(['2610090000', '26100900', '26100904', '261009'].map(s => resolveScan(s, SCANS, NOW, {}))).toEqual(['2026-10-09', '2026-10-09', null, '2026-10-09T1236'])
    expect(scanNeighbors('2026-10-09T03', SCANS, {})).toEqual({ before: '2026-10-09', after: '2026-10-09T1236' })
  })
  it('a start that isn\'t the scan\'s own UTC day, or isn\'t an instant, is unknown', () => {
    expect([
      startKey('2026-10-09', '2026-10-10T00:10:00Z'),
      startKey('2026-10-09', '2026-10-08T23:59:00Z'),
      startKey('2026-10-09', 'junk'),
      startKey('2026-10-09', undefined),
      startKey('2026-10-09T1236', '2026-10-09T12:36:00Z'),
      startKey('2026-10-09', '2026-10-08T20:30:00-04:00'),
    ]).toEqual([null, null, null, null, null, '2026-10-09T0030'])
  })
})
