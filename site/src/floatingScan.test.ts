import { describe, expect, it } from 'vitest'
import { fmtScan, floatingScan, pendingNote, selectScan } from './scan'
import { decodeSel } from './scanSlug'

// gcs 10/9: the date-only run (indexed) and 12:36Z (published, not yet appended to the static index).
const SCANS = ['2026-10-09T1236', '2026-10-09', '2026-10-08'] // newest first, as scans.json
const ALL = ['2026-10-08', '2026-10-09', '2026-10-09T1236']
const BEHIND = ['2026-10-08', '2026-10-09']

describe('a filtered view with no ?d= opens on the newest scan the static index covers', () => {
  it('no `d`, the newest scan not yet indexed: the newest covered scan, the newer one pending', () => {
    expect(selectScan(undefined, SCANS, true, BEHIND)).toEqual({ asof: '2026-10-09', miss: null, pending: ['2026-10-09T1236'] })
  })
  it('no `d`, every scan indexed: the newest scan, nothing pending', () => {
    expect(selectScan(undefined, SCANS, true, ALL)).toEqual({ asof: '2026-10-09T1236', miss: null, pending: [] })
  })
  it('an explicit `d` on an unindexed scan resolves as always (its view then says it isn\'t indexed)', () => {
    expect([selectScan(decodeSel('2610091236'), SCANS, true, BEHIND), selectScan(decodeSel('261009'), SCANS, true, BEHIND)]).toEqual([
      { asof: '2026-10-09T1236', miss: null, pending: [] },
      { asof: '2026-10-09T1236', miss: null, pending: [] },
    ])
  })
  it('no preference (unfiltered, or not indexed-only), the list loading, nothing covered', () => {
    expect([
      floatingScan(SCANS, undefined),
      floatingScan(SCANS, null),
      floatingScan(SCANS, 'loading'),
      floatingScan(SCANS, []),
      floatingScan([], BEHIND),
    ]).toEqual([
      { scan: '2026-10-09T1236', pending: [] },
      { scan: '2026-10-09T1236', pending: [] },
      { scan: null, pending: [] },
      { scan: '2026-10-09T1236', pending: [] },
      { scan: null, pending: [] },
    ])
  })
  it('says which scan it searched and which are still being indexed', () => {
    expect([
      pendingNote('2026-10-09', ['2026-10-09T1236']),
      pendingNote('2026-10-08', ['2026-10-09T1236', '2026-10-09T0601', '2026-10-09']),
    ]).toEqual([
      `Searching ${fmtScan('2026-10-09')}, the newest indexed scan; ${fmtScan('2026-10-09T1236')} is still being indexed.`,
      `Searching ${fmtScan('2026-10-08')}, the newest indexed scan; ${fmtScan('2026-10-09')}, ${fmtScan('2026-10-09T0601')} and ${fmtScan('2026-10-09T1236')} are still being indexed.`,
    ])
  })
})
