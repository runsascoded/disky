import { describe, expect, it } from 'vitest'
import { fmtScan, scanGroups } from './scan'

// Sub-daily ids display in the viewer's local time, so which day a 00:01Z
// scan lands on depends on the machine running the test; the spec is the
// grouping invariant, stated against `fmtScan` itself.
describe('scanGroups', () => {
  const now = new Date(Date.UTC(2026, 8, 17, 12))
  // the date-only ids sit days away from the sub-daily ones, so no local
  // rendering of a 00:01Z scan can land on their day
  const ids = ['2026-09-17T1201', '2026-09-17T0001', '2026-09-16T1201', '2026-09-16T0001', '2026-09-10', '2026-09-09']
  const groups = scanGroups(ids, now)
  it('keeps every scan, in order', () => {
    expect(groups.flatMap(g => g.scans.map(s => s.id))).toEqual(ids)
  })
  it('a group is the run of consecutive scans sharing a displayed day', () => {
    for (const g of groups) for (const s of g.scans) expect(fmtScan(s.id, now).split(' ')[0]).toBe(g.day)
    for (let i = 1; i < groups.length; i++) expect(groups[i].day).not.toBe(groups[i - 1].day)
  })
  it('sub-daily scans are labelled by time alone; a date-only scan is its own group labelled by its day', () => {
    for (const g of groups) for (const s of g.scans) {
      if (s.id.includes('T')) expect(s.label).toMatch(/^\d{1,2}:\d{2}[ap]$/)
      else expect([g.day, s.label, g.scans.length]).toEqual([fmtScan(s.id, now), fmtScan(s.id, now), 1])
    }
    expect(groups.slice(-2)).toEqual([{ day: '9/10', scans: [{ id: '2026-09-10', label: '9/10' }] }, { day: '9/9', scans: [{ id: '2026-09-09', label: '9/9' }] }])
  })
  it('empty in, empty out', () => {
    expect(scanGroups([])).toEqual([])
  })
})
