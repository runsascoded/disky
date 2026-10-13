import { describe, expect, it } from 'vitest'
import { hrefWithScan } from './NoScanMatch'
import { mapHref } from './scanRunsModel'
import { canonicalPrefix, canonicalSel, decodeSel, encodeSel, minPrefix, minSlug, namesForGood, resolveAfter, resolveScan, startKey, type ScanTimes } from './scanSlug'

const at = (d: number, h = 0, m = 0) => new Date(Date.UTC(2026, 9, d, h, m))
// gcs through 10/9: 10/7 and 10/8 date-only, alone on their days; 10/9's date-only run (started 04:30Z) and a manual
// 12:36Z scan; cw-style 10/6 with two scans in one hour (00:02, 00:41) and one at 06:01.
const SCANS = ['2026-10-06T0002', '2026-10-06T0041', '2026-10-06T0601', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-09T1236']
const TIMES: ScanTimes = { '2026-10-09': startKey('2026-10-09', '2026-10-09T04:30:12.345Z')! }
const NOW = at(9, 16)

describe('the canonical slug: the shortest permanent, unique prefix', () => {
  it('the slug table', () => {
    expect(SCANS.map(id => [id, minSlug(id, SCANS, TIMES, NOW)])).toEqual([
      ['2026-10-06T0002', '2610060002'], // two scans in hour 00: the minute
      ['2026-10-06T0041', '2610060041'],
      ['2026-10-06T0601', '26100606'],   // three on the day, alone in its hour
      ['2026-10-07', '261007'],          // alone on a past day
      ['2026-10-08', '261008'],
      ['2026-10-09', '26100904'],        // a date-only scan by its known start's hour
      ['2026-10-09T1236', '26100912'],
    ])
  })
  it('today\'s scan is not shortened before its period ends', () => {
    const scans = ['2026-10-08', '2026-10-09T1236']
    expect([at(9, 12, 40), at(9, 13), at(9, 23, 59), at(10)].map(now => minSlug('2026-10-09T1236', scans, {}, now)))
      .toEqual(['2610091236', '26100912', '26100912', '261009'])
  })
  it('a date-only scan with a known start keys by it; unknown, its day holds no hour slug', () => {
    expect([minSlug('2026-10-09', SCANS, TIMES, NOW), minSlug('2026-10-09', SCANS, {}, NOW), minSlug('2026-10-09T1236', SCANS, {}, NOW)])
      .toEqual(['26100904', '2610090000', '2610091236'])
    // alone on a past day, its start doesn't matter
    expect([minSlug('2026-10-08', SCANS, {}, NOW), minSlug('2026-10-08', ['2026-10-08'], {}, at(8, 20))]).toEqual(['261008', '2610080000'])
  })
  it('the midnight alias keeps a timed scan in hour 00 off the hour slug', () => {
    const scans = ['2026-10-09', '2026-10-09T0012']
    const times = { '2026-10-09': '2026-10-09T0430' }
    expect(scans.map(id => minSlug(id, scans, times, NOW))).toEqual(['26100904', '2610090012'])
  })
  it('a scan missing from the list falls back to its minute', () => {
    expect([minSlug('2026-10-05T1200', SCANS, TIMES, NOW), minSlug('2026-10-05', SCANS, TIMES, NOW)]).toEqual(['2610051200', '2610050000'])
  })
  it('each canonical slug resolves to its scan, and names it for good', () => {
    expect(SCANS.map(id => resolveScan(minSlug(id, SCANS, TIMES, NOW), SCANS, NOW, TIMES))).toEqual(SCANS)
    expect(SCANS.map(id => namesForGood(minPrefix(id, SCANS, TIMES, NOW), id, SCANS, TIMES, NOW))).toEqual(SCANS.map(() => true))
    expect(SCANS.map(id => resolveAfter(decodeSel(encodeSel({ d: minPrefix(id, SCANS, TIMES, NOW) })), SCANS, TIMES))).toEqual(SCANS)
  })
  it('old minute (and midnight) links still resolve to the same scan', () => {
    expect(['2610060601', '2610070000', '2610080000', '2610090000', '2610090430', '2610091236'].map(s => resolveScan(s, SCANS, NOW, TIMES)))
      .toEqual(['2026-10-06T0601', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-09', '2026-10-09T1236'])
  })
})

describe('URL canonicalization', () => {
  const canon = (qs: string, now = NOW, times = TIMES) => canonicalSel(new URLSearchParams(qs), now, SCANS, times)
  it('a pin that names one scan for good becomes its shortest slug; anything else is left alone', () => {
    expect([
      'd=2610080000',          // an old minute link → the day
      'd=2026-10-08',          // ISO → the day
      'd=2610091236',          // → the hour
      'd=2610090000',          // the midnight alias → the start's hour
      'd=2610091236-2610080000', // both endpoints
      'd=2610091236-7d',
      'd=261009',              // a day holding two scans: that day's latest, as is
      'd=26100600',            // an hour holding two: as is
      'd=2610060041',          // a minute in a shared hour: as is
      'd=26100903',            // a miss: as is
      'd=junk',
      'date=2026-10-08',       // a legacy key folds in, then shortens
    ].map(qs => canon(qs))).toEqual([
      '261008', '261008', '26100912', '26100904', '26100912-261008', '26100912-7d', '261009', '26100600', '2610060041', '26100903', 'junk', '261008',
    ])
  })
  it('today, before its period ends, a pin keeps its minute; never lengthened', () => {
    expect([canon('d=2610091236', at(9, 12, 50)), canon('d=26100912', at(9, 12, 50)), canonicalPrefix('2026-10-09T12', SCANS, {}, NOW)])
      .toEqual(['2610091236', '26100912', '2026-10-09T12'])
  })
  it('without the scan list (the OG view key), only the spelling canonicalizes', () => {
    expect(canonicalSel(new URLSearchParams('d=2610080000'), NOW)).toBe('2610080000')
  })
})

describe('links written from a scan id', () => {
  it('the nearest-scan links and the scan-runs map links', () => {
    expect([
      hrefWithScan('/', '?d=26100903', decodeSel('26100903'), '2026-10-09', true, TIMES, SCANS),
      hrefWithScan('/', '?d=26100903', decodeSel('26100903'), '2026-10-08', true, TIMES, SCANS),
      hrefWithScan('/', '?d=2610091236-26100903', decodeSel('2610091236-26100903'), '2026-10-09', false, TIMES, SCANS),
      hrefWithScan('/', '?d=26100903', decodeSel('26100903'), '2026-10-08', true, TIMES),
    ]).toEqual(['/?d=26100904', '/?d=261008', '/?d=2610091236-26100904', '/?d=2610080000'])
    const slug = (id: string) => minSlug(id, SCANS, TIMES, NOW)
    expect([mapHref('2026-10-08', '/', slug), mapHref('2026-10-09T1236', '/', slug), mapHref('2026-10-08')]).toEqual(['/?d=261008', '/?d=26100912', '/?d=2610080000'])
  })
})
