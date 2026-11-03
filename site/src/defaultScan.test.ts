import { describe, expect, it } from 'vitest'
import { defaultScan, defaultScanMode, indexingScans, prefersIndexed, searchOff } from './defaultScan'
import { fmtScan, pendingNote } from './scan'

// gcs 10/9: the date-only run (indexed) and 12:36Z (published, not yet appended to the static index).
const SCANS = ['2026-10-09T1236', '2026-10-09', '2026-10-08'] // newest first, as scans.json
const BEHIND = ['2026-10-08', '2026-10-09']

describe('`DEFAULT_SCAN`: the scan a view with no `?d=` opens on', () => {
  it('reads `newest` (unset, blank) or `searchable`; anything else is a config error', () => {
    expect([defaultScanMode(undefined), defaultScanMode(''), defaultScanMode(' searchable '), defaultScanMode('newest')]).toEqual(['newest', 'newest', 'searchable', 'newest'])
    expect(() => defaultScanMode('default')).toThrow(new Error('DEFAULT_SCAN=default: want newest | searchable'))
  })
  it('a filtered view prefers the covered scans in both modes; an unfiltered one only on `searchable`', () => {
    expect([prefersIndexed('newest', false), prefersIndexed('newest', true), prefersIndexed('searchable', false), prefersIndexed('searchable', true)])
      .toEqual([false, true, true, true])
  })
  it('`newest`: the newest published scan unfiltered, the newest covered one filtered', () => {
    expect([defaultScan(SCANS, BEHIND, 'newest', false), defaultScan(SCANS, BEHIND, 'newest', true)]).toEqual([
      { scan: '2026-10-09T1236', pending: [] },
      { scan: '2026-10-09', pending: ['2026-10-09T1236'] },
    ])
  })
  it('`searchable`: the newest covered scan either way; no static filter (null) or nothing covered: the newest; the list loading: none yet', () => {
    expect([
      defaultScan(SCANS, BEHIND, 'searchable', false),
      defaultScan(SCANS, BEHIND, 'searchable', true),
      defaultScan(SCANS, null, 'searchable', false),
      defaultScan(SCANS, [], 'searchable', false),
      defaultScan(SCANS, 'loading', 'searchable', false),
      defaultScan(SCANS, 'loading', 'newest', false),
    ]).toEqual([
      { scan: '2026-10-09', pending: ['2026-10-09T1236'] },
      { scan: '2026-10-09', pending: ['2026-10-09T1236'] },
      { scan: '2026-10-09T1236', pending: [] },
      { scan: '2026-10-09T1236', pending: [] },
      { scan: null, pending: [] },
      { scan: '2026-10-09T1236', pending: [] },
    ])
  })
})

describe('a scan the index does not cover yet: the search box is off, the picker marks it', () => {
  it('off only for a scan the covered list leaves out; on while the list loads or without a static filter', () => {
    expect([
      searchOff('2026-10-09T1236', BEHIND),
      searchOff('2026-10-09', BEHIND),
      searchOff('2026-10-09T1236', 'loading'),
      searchOff('2026-10-09T1236', null),
      searchOff(null, BEHIND),
    ]).toEqual([true, false, false, false, false])
  })
  it('"indexing": the scans newer than the newest covered one, never an older one before the index\'s base', () => {
    expect([
      indexingScans(SCANS, BEHIND),
      indexingScans(SCANS, ['2026-10-09']),
      indexingScans(SCANS, null),
      indexingScans(SCANS, 'loading'),
      indexingScans(SCANS, []),
    ]).toEqual([['2026-10-09T1236'], ['2026-10-09T1236'], [], [], []])
  })
  it('an unfiltered view that skipped newer scans says it shows the newest searchable one', () => {
    expect(pendingNote('2026-10-09', ['2026-10-09T1236'], undefined, false))
      .toBe(`Showing ${fmtScan('2026-10-09')}, the newest searchable scan; ${fmtScan('2026-10-09T1236')} is still being indexed.`)
  })
})
