/** The change strip's bars (specs/change-metrics.md §4.2): flows summed per UTC day or ISO week, uncounted scans absent. */
import { describe, expect, it } from 'vitest'
import { barExtent, bucketFor, changeBars, type FlowPoint } from './changeBars'
import { barText } from './ChangeStrip'
import { DAY } from './scan'

const f = (date: string, added: number, deleted: number, modified: number, since?: string): FlowPoint =>
  ({ date, added, deleted, modified, ...(since ? { since } : {}) })
// Two scans on Mon 11/16 (one since a scan the series lacks), none counted on 11/17 (the first scan: no flows), one on
// Sun 11/22 and one on Mon 11/23 (the next ISO week).
const POINTS: FlowPoint[] = [
  { date: '2026-11-17T0700' },
  f('2026-11-16T1900', 5, 2, 1),
  f('2026-11-16T0700', 10, 0, 3, '2026-11-14T0700'),
  f('2026-11-22T0700', 0, 40, 0),
  f('2026-11-23T0700', 1, 1, 1),
]
const day = (d: number) => Date.UTC(2026, 10, d)

describe('changeBars', () => {
  it('sums each UTC day\'s counted scans (oldest first), net = added − deleted, the earliest `since`', () => {
    expect(changeBars(POINTS, 'day')).toEqual([
      { t: day(16), added: 15, deleted: 2, modified: 4, net: 13, scans: ['2026-11-16T0700', '2026-11-16T1900'], since: '2026-11-14T0700' },
      { t: day(22), added: 0, deleted: 40, modified: 0, net: -40, scans: ['2026-11-22T0700'] },
      { t: day(23), added: 1, deleted: 1, modified: 1, net: 0, scans: ['2026-11-23T0700'] },
    ])
  })
  it('per ISO week (Monday first): Sunday the 22nd in the week of the 16th', () => {
    expect(changeBars(POINTS, 'week')).toEqual([
      { t: day(16), added: 15, deleted: 42, modified: 4, net: -27, scans: ['2026-11-16T0700', '2026-11-16T1900', '2026-11-22T0700'], since: '2026-11-14T0700' },
      { t: day(23), added: 1, deleted: 1, modified: 1, net: 0, scans: ['2026-11-23T0700'] },
    ])
  })
  it('no counted scan: no bars', () => {
    expect(changeBars([{ date: '2026-11-17T0700' }], 'day')).toEqual([])
  })
  it('weeks past 90 days; the extent above (written) and below (deleted) the zero line', () => {
    expect([bucketFor(90 * DAY), bucketFor(90 * DAY + 1)]).toEqual(['day', 'week'])
    expect(barExtent(changeBars(POINTS, 'day'))).toEqual({ up: 19, down: 40 })
  })
  it("a bar's tooltip line", () => {
    const [a, b] = changeBars(POINTS, 'day')
    expect([barText(a, false), barText(b, false), barText(changeBars(POINTS, 'week')[0], true)]).toEqual([
      'Nov 16 · +15 added · −2 deleted · ~4 modified · net +13 (2 scans) · since 2026-11-14T0700',
      'Nov 22 · +0 added · −40 deleted · ~0 modified · net −40',
      'wk of Nov 16 · +15 added · −42 deleted · ~4 modified · net −27 (3 scans) · since 2026-11-14T0700',
    ])
  })
})
