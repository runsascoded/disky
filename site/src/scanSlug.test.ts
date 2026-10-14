import { describe, expect, it } from 'vitest'
import { DAY, RUN_ID_RE, canonicalSel, exactPrefix, exactSlug, scanNeighbors, decodeScan, decodeSel, encodeScan, encodeSel, isScanId, latestScan, mergeSel, resolveAfter, resolveBefore, resolveScan, scanMatches, selOf } from './scanSlug'

// Two scans on 10/9 (plus a third in its noon hour), a date-only scan on 10/8.
const SCANS = ['2026-10-08', '2026-10-09T0601', '2026-10-09T1200', '2026-10-09T1215', '2026-10-09T1802']
const NOW = new Date(Date.UTC(2026, 9, 9))

describe('resolveScan — the latest scan whose id starts with the slug (spec §2)', () => {
  const table: [string, string | null][] = [
    // a day: its latest scan
    ['261009', '2026-10-09T1802'],
    ['2026-10-09', '2026-10-09T1802'],
    // an hour: its latest scan
    ['26100912', '2026-10-09T1215'],
    ['261009-12', '2026-10-09T1215'],
    ['261009T12', '2026-10-09T1215'],
    ['2026-10-09T12', '2026-10-09T1215'],
    // a minute: exactly that scan
    ['2610091200', '2026-10-09T1200'],
    ['261009-1200', '2026-10-09T1200'],
    ['2610090601', '2026-10-09T0601'],
    ['2026-10-09T1200', '2026-10-09T1200'],
    ['261009-0601', '2026-10-09T0601'],
    // a date-only scan is its day's (only) match
    ['261008', '2026-10-08'],
    ['2026-10-08', '2026-10-08'],
    // year-less spelling (current UTC year)
    ['10-9', '2026-10-09T1802'],
    ['10-9-6', '2026-10-09T0601'],
    // nothing matches / not a slug
    ['261010', null],
    ['26100913', null],
    ['26100903', null],
    ['261009-13', null],
    ['20261009', null],
    ['2026-10-09T1201', null],
    ['261399', null],
    ['2026-02-30', null],
    ['junk', null],
    ['', null],
  ]
  for (const [slug, scan] of table) it(`${JSON.stringify(slug)} → ${scan}`, () => expect(resolveScan(slug, SCANS, NOW)).toBe(scan))
  it('is order-independent', () => {
    expect(resolveScan('261009', [...SCANS].reverse(), NOW)).toBe('2026-10-09T1802')
  })
})

describe('decodeScan / encodeScan — slug ↔ id prefix', () => {
  it('decodes every spelling to its ISO prefix', () => {
    expect(['261009', '26100912', '2610091200', '261009-12', '261009-1200', '261009T1200', '2026-10-09', '2026-10-09T12', '2026-10-09T1200', '10-9-12', '26-10-9-12', '2026-10-9-12-00', '26100924', '261009-24', '2026-13-01', '1009', '2610091', '261009123']
      .map(s => decodeScan(s, NOW))).toEqual([
      '2026-10-09', '2026-10-09T12', '2026-10-09T1200', '2026-10-09T12', '2026-10-09T1200', '2026-10-09T1200', '2026-10-09', '2026-10-09T12', '2026-10-09T1200', '2026-10-09T12', '2026-10-09T12', '2026-10-09T1200',
      undefined, undefined, undefined, undefined, undefined, undefined,
    ])
  })
  it('encodes ids and prefixes compactly and dashless: YYMMDD[HH[MM]]', () => {
    expect(['2026-10-09', '2026-10-09T12', '2026-10-09T1236', undefined, 'other'].map(encodeScan)).toEqual(['261009', '26100912', '2610091236', undefined, 'other'])
  })
  it('isScanId: full, calendar-valid ids only', () => {
    expect(['2026-10-09', '2026-10-09T1200', '2026-10-09T12', '2026-10-09T2400', '2026-02-30', '0000-01-01', '261009', 20261009].map(isScanId))
      .toEqual([true, true, false, false, false, false, false, false])
  })
  it('scanMatches / latestScan take a decoded prefix', () => {
    expect(scanMatches('2026-10-09T12', SCANS)).toEqual(['2026-10-09T1215', '2026-10-09T1200'])
    expect(latestScan('2026-10-09', SCANS)).toBe('2026-10-09T1802')
    expect(latestScan(undefined, SCANS)).toBe(null)
  })
})

describe('the ?d= codec round-trips (spec §3)', () => {
  const canonical = ['261009', '26100912', '2610091200', '-7d', '-6d12h', '2610091200-2d', '-261005', '261009-261005', '2610091802-2610090601', '26100912-261005']
  for (const enc of canonical) it(`round-trips ${enc}`, () => expect(encodeSel(decodeSel(enc, NOW))).toBe(enc))
  it('ISO spellings decode and re-encode to the compact form', () => {
    expect(['2026-10-09', '2026-10-09T12', '2026-10-09T1200', '2026-10-06', '261009-12', '261009-1200'].map(e => encodeSel(decodeSel(e, NOW)))).toEqual(['261009', '26100912', '2610091200', '261006', '26100912', '2610091200'])
  })
})

describe('legacy links canonicalize to ?d= (back-compat)', () => {
  const table: [string, string | undefined][] = [
    ['date=2026-10-09&name=gof', '261009'],
    ['date=2026-10-09T1200', '2610091200'],
    ['date=junk', 'junk'],
    ['date=2026-10-09&from=2026-10-05', '261009-261005'],
    ['from=2026-10-05', '-261005'],
    ['d=2026-10-06', '261006'],
    ['d=261009', '261009'],
    // `d`'s own parts win over the legacy keys; a span excludes a `from`
    ['d=261009&date=2026-10-01', '261009'],
    ['d=-7d&from=2026-10-01', '-7d'],
    ['d=-261003&from=2026-10-01', '-261003'],
    ['name=gof', undefined],
    ['d=junk', 'junk'],
    // an unparseable value wins over a good legacy key: the page shows the miss
    ['d=junk&date=2026-10-09', 'junk'],
  ]
  for (const [qs, d] of table) it(`?${qs} → d=${d}`, () => expect(canonicalSel(new URLSearchParams(qs), NOW)).toBe(d))
  it('mergeSel folds the decoded keys', () => {
    expect(mergeSel({ date: { d: '2026-10-09' }, from: { from: '2026-10-05' } })).toEqual({ d: '2026-10-09', from: '2026-10-05' })
    expect(mergeSel({})).toBe(undefined)
  })
  it('selOf reads the same merge', () => {
    expect(selOf(new URLSearchParams('date=2026-10-09T12&from=261008'), NOW)).toEqual({ d: '2026-10-09T12', from: '2026-10-08' })
  })
})

describe('resolveAfter / resolveBefore — two scans on one day are each addressable', () => {
  it('a day picks its later scan; each scan by its own id', () => {
    expect([undefined, { d: '2026-10-09' }, { d: '2026-10-09T0601' }, { d: '2026-10-09T1802' }, { d: '2026-10-10' }].map(s => resolveAfter(s, SCANS)))
      .toEqual(['2026-10-09T1802', '2026-10-09T1802', '2026-10-09T0601', '2026-10-09T1802', null])
  })
  it('a pinned `from` resolves among the earlier scans; a span to the nearest', () => {
    const after = '2026-10-09T1802'
    expect([
      resolveBefore({ from: '2026-10-09' }, after, SCANS),
      resolveBefore({ from: '2026-10-08' }, after, SCANS),
      resolveBefore({ from: '2026-10-10' }, after, SCANS),
      resolveBefore({ span: 12 * DAY / 24 }, after, SCANS),
      resolveBefore({ span: 2 * DAY }, after, SCANS),
      resolveBefore(undefined, after, SCANS),
    ]).toEqual(['2026-10-09T1215', '2026-10-08', null, '2026-10-09T0601', '2026-10-08', undefined])
  })
})

describe('RUN_ID_RE — a sweep run keyed by its scan id', () => {
  it('accepts date-only and sub-daily scan ids', () => {
    expect(['2026-09-28-p1/20260928T120000Z', '2026-10-09T0601-p12/20261009T120000Z', '2026-10-09T06-p1/20261009T120000Z', '2026-10-09-p1/x'].map(id => RUN_ID_RE.test(id)))
      .toEqual([true, true, false, false])
  })
})

describe('a miss is a miss: no fallback to the latest or nearest scan', () => {
  it('resolveAfter is null for an unmatched or invalid selection', () => {
    expect([{ d: '2026-10-09T03' }, { d: '2026-10-10' }, { invalid: 'junk' }].map(s => resolveAfter(s, SCANS))).toEqual([null, null, null])
  })
  it('scanNeighbors names the closest scans on either side of a missed prefix', () => {
    expect([
      scanNeighbors('2026-10-09T03', SCANS),
      scanNeighbors('2026-10-10', SCANS),
      scanNeighbors('2026-10-07', SCANS),
      scanNeighbors('2026-10-09T13', SCANS),
      scanNeighbors('2026-10-09T03', []),
    ]).toEqual([
      { before: '2026-10-08', after: '2026-10-09T0601' },
      { before: '2026-10-09T1802', after: null },
      { before: null, after: '2026-10-08' },
      { before: '2026-10-09T1215', after: '2026-10-09T1802' },
      { before: null, after: null },
    ])
  })
})

describe('a date-only scan and a timed scan on one day: each exactly selectable', () => {
  // gcs on 10/9: the date-only 04:30 run, and 12:36Z
  const DAY2 = ['2026-10-08', '2026-10-09', '2026-10-09T1236']
  it('exactSlug: a date-only scan is its midnight, a timed one its minute', () => {
    expect(DAY2.map(id => exactSlug(id))).toEqual(['2610080000', '2610090000', '2610091236'])
  })
  it('every scan round-trips: its exact slug → ?d= → decode → the resolver → itself', () => {
    expect(DAY2.map(id => resolveAfter(decodeSel(encodeSel({ d: exactPrefix(id) })), DAY2))).toEqual(DAY2)
    expect(DAY2.map(id => resolveScan(exactSlug(id), DAY2))).toEqual(DAY2)
  })
  it('as a pinned start too', () => {
    expect(['2026-10-08', '2026-10-09'].map(id => resolveBefore(decodeSel(encodeSel({ d: '2026-10-09T1236', from: exactPrefix(id) })), '2026-10-09T1236', DAY2)))
      .toEqual(['2026-10-08', '2026-10-09'])
  })
  it('the day slug keeps meaning the day\'s latest scan; the midnight hour takes the date-only one', () => {
    expect(['261009', '2026-10-09', '26100900', '2610090000', '26100912', '2610091236', '26100904'].map(s => resolveScan(s, DAY2)))
      .toEqual(['2026-10-09T1236', '2026-10-09T1236', '2026-10-09', '2026-10-09', '2026-10-09T1236', '2026-10-09T1236', null])
  })
  it('scanMatches / scanNeighbors order a date-only scan as its midnight', () => {
    expect([scanMatches('2026-10-09', DAY2), scanNeighbors('2026-10-09T04', DAY2), scanNeighbors('2026-10-09T00', ['2026-10-08', '2026-10-09T1236'])]).toEqual([
      ['2026-10-09T1236', '2026-10-09'],
      { before: '2026-10-09', after: '2026-10-09T1236' },
      { before: '2026-10-08', after: '2026-10-09T1236' },
    ])
  })
})
