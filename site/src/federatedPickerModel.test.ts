import { describe, expect, it } from 'vitest'
import { adjacentCutoffs, cutoffCoverage, cutoffDays, fmtCutoff, markOf, sourcesAt, type PickerSource } from './federatedPickerModel'
import { parseSourceCatalog } from './federatedScans'

const catalogs = ['awair', 'ctbk'].map((source, i) => parseSourceCatalog({
  version: 1, source, label: source === 'awair' ? 'Awair' : 'Bike data', mount: source,
  scope: { store: `r2:rac/${source}`, prefix: '' },
  scans: [0, 1].map(day => ({
    id: `${source}-${day}`, revision: '1', digest: String(i + day).repeat(64), complete: true,
    observed_from: `2026-10-${12 + day}T0${i + 2}:00:00Z`,
    observed_through: `2026-10-${12 + day}T0${i + 2}:00:00Z`,
    published_at: `2026-10-${12 + day}T0${i + 3}:00:00Z`,
    total_bytes: 100, total_objects: 2, artifacts: { format: 'path-store-v2', path: `scans/${day}/path.parquet` },
  })),
}))
describe('federated cutoff picker model', () => {
  it('groups by viewer-local day without modifying exact UTC event values', () => {
    expect(cutoffDays(catalogs, 'America/New_York')).toEqual([
      { day: '2026-10-12', events: [
        { at: '2026-10-13T03:00:00.000Z', time: '23:00', sources: [{ source: 'ctbk', label: 'Bike data' }] },
        { at: '2026-10-13T02:00:00.000Z', time: '22:00', sources: [{ source: 'awair', label: 'Awair' }] },
      ] },
      { day: '2026-10-11', events: [
        { at: '2026-10-12T03:00:00.000Z', time: '23:00', sources: [{ source: 'ctbk', label: 'Bike data' }] },
        { at: '2026-10-12T02:00:00.000Z', time: '22:00', sources: [{ source: 'awair', label: 'Awair' }] },
      ] },
    ])
  })
  it('navigates strict prior/next events, including arbitrary non-event cutoffs', () => {
    expect(adjacentCutoffs(catalogs, '2026-10-13T02:30:00Z')).toEqual({ previous: '2026-10-13T02:00:00.000Z', next: '2026-10-13T03:00:00.000Z' })
    expect(adjacentCutoffs(catalogs, '2026-10-13T03:00:00Z')).toEqual({ previous: '2026-10-13T02:00:00.000Z', next: null })
    expect(adjacentCutoffs(catalogs, '2026-10-11T00:00:00Z')).toEqual({ previous: null, next: '2026-10-12T02:00:00.000Z' })
  })
  it('deduplicates simultaneous events and retains both source labels', () => {
    const tied = { ...catalogs[1], scans: [catalogs[0].scans[0]] }
    expect(cutoffDays([catalogs[0], tied], 'UTC')).toEqual([
      { day: '2026-10-13', events: [{ at: '2026-10-13T02:00:00.000Z', time: '02:00', sources: [{ source: 'awair', label: 'Awair' }] }] },
      { day: '2026-10-12', events: [{ at: '2026-10-12T02:00:00.000Z', time: '02:00', sources: [{ source: 'awair', label: 'Awair' }, { source: 'ctbk', label: 'Bike data' }] }] },
    ])
  })
  it('unknown sources do not count as observed; empty catalogs stay honest', () => {
    expect(cutoffCoverage(catalogs, '2026-10-12T02:30:00Z')).toEqual({ ready: 1, total: 2 })
    expect(cutoffCoverage(catalogs, '2026-10-11T00:00:00Z')).toEqual({ ready: 0, total: 2 })
    expect(cutoffDays([], 'UTC')).toEqual([])
    expect(adjacentCutoffs([], '2026-10-13T00:00:00Z')).toEqual({ previous: null, next: null })
  })
  it('shows minutes, never seconds; the exact instant is untouched', () => {
    expect([
      fmtCutoff('2026-10-13T02:00:59.999Z', 'UTC'),
      fmtCutoff('2026-10-13T02:07:30.000Z', 'America/New_York'),
      fmtCutoff('2026-10-13T14:30:00.000Z', 'UTC'),
    ]).toEqual(['Oct 13, 2:00 AM', 'Oct 12, 10:07 PM', 'Oct 13, 2:30 PM'])
    const odd = { ...catalogs[0], scans: [{ ...catalogs[0].scans[0], observed_through: '2026-10-12T02:00:42.000Z' }] }
    expect(cutoffDays([odd], 'UTC')).toEqual([
      { day: '2026-10-12', events: [{ at: '2026-10-12T02:00:42.000Z', time: '02:00', sources: [{ source: 'awair', label: 'Awair' }] }] },
    ])
  })
  it('a calendar-day cutoff (a date-only scan) shows its date, never a viewer-local time; a timed one at the same instant keeps its time', () => {
    const day = (id: string) => ({ id, revision: '', observed_through: `${id}T00:00:00.000Z`, published_at: `${id}T00:00:00.000Z`, day: true as const })
    const r2: PickerSource[] = [{ source: 'r2', label: 'R2', scans: [day('2026-10-14'), day('2026-10-13')] }]
    expect([
      fmtCutoff('2026-10-14T00:00:00.000Z', 'America/New_York', true),
      fmtCutoff('2026-10-14T00:00:00.000Z', 'America/New_York'),
      cutoffDays(r2, 'America/New_York').map(g => [g.day, g.events.map(e => e.time)]),
      cutoffDays([...r2, { source: 'x', label: 'X', scans: [{ ...day('2026-10-14'), day: undefined }] }], 'UTC')[0].events.map(e => [e.time, e.day ?? null]),
    ]).toEqual([
      'Oct 14',
      'Oct 13, 8:00 PM',
      [['2026-10-14', ['Oct 14']], ['2026-10-13', ['Oct 13']]],
      [['00:00', null]],
    ])
  })
  it('a source with an icon carries it on its chips and its mark; one without falls back to its label', () => {
    const iconed: PickerSource[] = [{ ...catalogs[0], icon: '/icons/awair.svg' }, catalogs[1]]
    expect(cutoffDays(iconed, 'UTC').at(-1)).toEqual({ day: '2026-10-12', events: [
      { at: '2026-10-12T03:00:00.000Z', time: '03:00', sources: [{ source: 'ctbk', label: 'Bike data' }] },
      { at: '2026-10-12T02:00:00.000Z', time: '02:00', sources: [{ source: 'awair', label: 'Awair', icon: '/icons/awair.svg' }] },
    ] })
    expect(iconed.map(markOf)).toEqual([{ source: 'awair', label: 'Awair', icon: '/icons/awair.svg' }, { source: 'ctbk', label: 'Bike data' }])
  })
  it('each source’s scan at a cutoff: its latest observed through it, null before its first', () => {
    // A union member's scans (`/api/union-scans`): ids and instants, no digests or capture windows.
    const members: PickerSource[] = [
      { source: 'primary', label: 'Buckets', scans: [{ id: 'A', revision: '', observed_through: '2026-10-01T00:01:00.000Z', published_at: '2026-10-01T00:01:00.000Z' }] },
      { source: 'meta', label: 'Index', icon: 'https://example.com/i.png', scans: [{ id: 'B', revision: '', observed_through: '2026-10-02T00:01:00.000Z', published_at: '2026-10-02T00:01:00.000Z' }] },
    ]
    expect(sourcesAt(members, '2026-10-01T12:00:00Z').map(s => [s.source.source, s.scan?.id ?? null])).toEqual([['meta', null], ['primary', 'A']])
    expect(sourcesAt(members, '2026-10-02T00:01:00Z').map(s => [s.source.source, s.scan?.id ?? null])).toEqual([['meta', 'B'], ['primary', 'A']])
  })
})
