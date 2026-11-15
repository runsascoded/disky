import { describe, expect, it } from 'vitest'
import { classTotals, type NonLiveRow, type NonLiveSeries, stackedBands } from './nonLiveModel'

const row = (bucket: string, cls: NonLiveRow['class'], bytes: number, objects: number, expires_max: string | null = null): NonLiveRow =>
  ({ bucket, class: cls, prefix: '', bytes, objects, oldest: null, newest: null, expires_min: null, expires_max })

describe('classTotals', () => {
  it('sums each class over buckets in label order, skips empty classes, and knows an expiry only when every bucket has one', () => {
    expect(classTotals([
      row('b', 'mpu', 5, 1),
      row('a', 'soft-deleted', 40, 2, '2026-10-25T20:48:18Z'),
      row('b', 'soft-deleted', 10, 1, '2026-10-24T02:18:51Z'),
      row('a', 'mpu', 0, 0),
      row('c', 'noncurrent', 7, 3, '2026-11-01T00:00:00Z'),
      row('d', 'noncurrent', 1, 1),
    ])).toEqual([
      { cls: 'soft-deleted', bytes: 50, objects: 3, buckets: ['a', 'b'], expires: '2026-10-25T20:48:18Z' },
      { cls: 'noncurrent', bytes: 8, objects: 4, buckets: ['c', 'd'], expires: null },
      { cls: 'mpu', bytes: 5, objects: 1, buckets: ['b'], expires: null },
    ])
  })

  it('is empty when nothing is held', () => {
    expect(classTotals([row('a', 'soft-deleted', 0, 0)])).toEqual([])
  })
})

describe('stackedBands', () => {
  const x = (d: string) => Date.parse(`${d}T00:00:00Z`)
  it('sums buckets per day and stacks live under each non-live class, dropping leading empty days and marking later ones gaps', () => {
    const s: NonLiveSeries = {
      as_of: '2026-11-15T08:00:00Z', metric: 'm',
      dates: ['2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13'],
      buckets: {
        a: { live: [null, 100, null, 90], 'soft-deleted': [null, 5, null, 0], mpu: [null, 0, null, 2] },
        b: { live: [null, null, null, 10], noncurrent: [null, null, null, 0] },
      },
    }
    expect(stackedBands(s)).toEqual({
      bands: [
        { cls: 'live', points: [{ x: x('2026-11-11'), y0: 0, y: 100 }, { x: x('2026-11-13'), y0: 0, y: 100 }] },
        { cls: 'soft-deleted', points: [{ x: x('2026-11-11'), y0: 100, y: 105 }, { x: x('2026-11-13'), y0: 100, y: 100 }] },
        { cls: 'mpu', points: [{ x: x('2026-11-11'), y0: 105, y: 105 }, { x: x('2026-11-13'), y0: 100, y: 102 }] },
      ],
      gaps: [x('2026-11-12')],
    })
  })
})
