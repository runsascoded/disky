// /admin/cost/gcp's model over two synthetic days in `deploy_cost.snapshot`'s shape: $/month by component, the
// stacking order across the window, the trend, projections (storage's plus compute flat), and buckets by cost.
import { describe, expect, it } from 'vitest'
import {
  bucketNotes, bucketsByCost, componentSeries, components, type GcpBucket, type GcpCostSnapshot, type GcpTally,
  lastSnapshots, projections, trend,
} from './costModel'

const G = 1e9
const tally = (jobs: number, usd_per_month: number): GcpTally => ({ jobs, vm_hours: jobs * 2, usd: usd_per_month / 15, usd_per_month })

const bucket = (name: string, bytes: number, usd: number, extra: Partial<GcpBucket> = {}): GcpBucket => ({
  bucket: name, bytes, bytes_by_type: { 'live-object': bytes }, bytes_by_class: { REGIONAL: bytes },
  live_slope_bytes_per_day: 0, usd_per_month: usd, unpriced_bytes: 0,
  horizons: { '1y': { bytes, usd_per_month: usd } }, points: 30, short: false, ttl_days: null, soft_delete_off: false,
  partial_ttl: false, ...extra,
})

const day = (date: string, storageUsd: number, comps: Record<string, GcpTally> | null, extra: Partial<GcpCostSnapshot> = {}): GcpCostSnapshot => {
  const computeUsd = comps ? Object.values(comps).reduce((t, c) => t + c.usd_per_month, 0) : 0
  return {
    version: 1, cloud: 'gcp', date, name: 'd', project: 'p', as_of: `${date}T07:00:00Z`, window_days: 30,
    horizons: { '1y': 365.25, '2y': 730.5 }, basis: 'b', summary: 's',
    usd_per_month: { storage: storageUsd, compute: computeUsd, total: storageUsd + computeUsd },
    storage: {
      bytes: 300 * G, bytes_by_type: { 'live-object': 300 * G }, live_slope_bytes_per_day: G, usd_per_month: storageUsd,
      unpriced_bytes: {}, horizons: { '1y': { bytes: 665 * G, usd_per_month: storageUsd * 2 }, '2y': { bytes: 1030 * G, usd_per_month: storageUsd * 3 } },
      rows: [], buckets: [bucket('b-small', 10 * G, 0.2), bucket('b-big', 200 * G, 4), bucket('b-mid', 90 * G, 4)],
    },
    compute: comps && {
      selector: { app: 'disky', deployment: 'd' }, jobs: 0, vm_hours: 0, usd: 0, usd_per_day: 0, usd_per_month: computeUsd,
      components: comps, machines: {}, unpriced_vm_hours: {}, unlabeled: {},
    },
    ...extra,
  }
}

const D1 = day('2026-10-24', 8, { scan: tally(30, 40), drill: tally(2, 5) })
const D2 = day('2026-10-25', 9, { scan: tally(31, 42), anchors: tally(1, 60) })

describe('gcp cost model', () => {
  it('takes the newest days, oldest first', () => {
    expect(lastSnapshots([D2, D1], 1).map(s => s.date)).toEqual(['2026-10-25'])
    expect(lastSnapshots([D2, D1], 7).map(s => s.date)).toEqual(['2026-10-24', '2026-10-25'])
  })

  it('lists $/month by component, most first', () => {
    expect(components(D1)).toEqual([
      { key: 'compute:scan', label: 'Batch scan', usd: 40 },
      { key: 'storage', label: 'Storage', usd: 8 },
      { key: 'compute:drill', label: 'Batch drill', usd: 5 },
    ])
    expect(components(day('2026-10-24', 3, null))).toEqual([{ key: 'storage', label: 'Storage', usd: 3 }])
  })

  it('stacks storage first, then components by their window total', () => {
    // scan 82, anchors 60, drill 5
    expect(componentSeries([D1, D2]).map(s => [s.key, s.color])).toEqual([
      ['storage', 'var(--s1)'], ['compute:scan', 'var(--s5)'], ['compute:anchors', 'var(--s4)'], ['compute:drill', 'var(--s7)'],
    ])
  })

  it('traces each day\'s $/month by component', () => {
    expect(trend([D1, D2])).toEqual([
      { date: '2026-10-24', total: 53, byKey: { 'compute:scan': 40, storage: 8, 'compute:drill': 5 }, storageBytes: 300 * G },
      { date: '2026-10-25', total: 111, byKey: { 'compute:anchors': 60, 'compute:scan': 42, storage: 9 }, storageBytes: 300 * G },
    ])
  })

  it('projects storage per horizon and compute flat', () => {
    expect(projections(D1)).toEqual([
      { horizon: '1y', days: 365.25, storageBytes: 665 * G, storageUsd: 16, computeUsd: 45, totalUsd: 61 },
      { horizon: '2y', days: 730.5, storageBytes: 1030 * G, storageUsd: 24, computeUsd: 45, totalUsd: 69 },
    ])
    expect(projections(day('2026-10-24', 0, { scan: tally(1, 10) }, { storage: null }))).toEqual([
      { horizon: '1y', days: 365.25, storageBytes: 0, storageUsd: 0, computeUsd: 10, totalUsd: 10 },
      { horizon: '2y', days: 730.5, storageBytes: 0, storageUsd: 0, computeUsd: 10, totalUsd: 10 },
    ])
  })

  it('orders buckets by cost, then size', () => {
    expect(bucketsByCost(D1).map(b => b.bucket)).toEqual(['b-big', 'b-mid', 'b-small'])
  })

  it('says what a bucket\'s projection assumes', () => {
    expect(bucketNotes(bucket('a', 1, 1))).toEqual(['30 daily points', 'soft-deleted bytes held'])
    expect(bucketNotes(bucket('a', 1, 1, { points: 3, short: true, ttl_days: 7, soft_delete_off: true }))).toEqual([
      'short window (3 daily points): provisional', 'lifecycle Delete at 7d: live bytes held flat', 'soft delete off: soft-deleted bytes drain',
    ])
    expect(bucketNotes(bucket('a', 1, 1, { partial_ttl: true }))).toEqual([
      '30 daily points', 'partially TTL\'d (scoped Delete rules): live slope kept', 'soft-deleted bytes held',
    ])
  })
})
