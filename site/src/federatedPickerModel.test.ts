import { describe, expect, it } from 'vitest'
import { adjacentCutoffs, cutoffCoverage, cutoffDays } from './federatedPickerModel'
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
        { at: '2026-10-13T03:00:00.000Z', time: '23:00:00', labels: ['Bike data'] },
        { at: '2026-10-13T02:00:00.000Z', time: '22:00:00', labels: ['Awair'] },
      ] },
      { day: '2026-10-11', events: [
        { at: '2026-10-12T03:00:00.000Z', time: '23:00:00', labels: ['Bike data'] },
        { at: '2026-10-12T02:00:00.000Z', time: '22:00:00', labels: ['Awair'] },
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
      { day: '2026-10-13', events: [{ at: '2026-10-13T02:00:00.000Z', time: '02:00:00', labels: ['Awair'] }] },
      { day: '2026-10-12', events: [{ at: '2026-10-12T02:00:00.000Z', time: '02:00:00', labels: ['Awair', 'Bike data'] }] },
    ])
  })
  it('unknown sources do not count as observed; empty catalogs stay honest', () => {
    expect(cutoffCoverage(catalogs, '2026-10-12T02:30:00Z')).toEqual({ ready: 1, total: 2 })
    expect(cutoffCoverage(catalogs, '2026-10-11T00:00:00Z')).toEqual({ ready: 0, total: 2 })
    expect(cutoffDays([], 'UTC')).toEqual([])
    expect(adjacentCutoffs([], '2026-10-13T00:00:00Z')).toEqual({ previous: null, next: null })
  })
})
