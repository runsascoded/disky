import { describe, expect, it } from 'vitest'
import { batchAnchor, historyPoints, runHref, type ProgressSample } from './runHistory'

const row = (bucket: string, ts: number, deletes: number, bytes = deletes * 10, done = 0): ProgressSample => ({ bucket, ts, deletes, bytes, gone: 0, overwritten: 0, failed: 0, done, bytes_exact: 1 })

describe('run progress series', () => {
  it('preserves bucket totals, derives interval rates, excludes finished bucket rates', () => {
    expect(historyPoints([row('a', 100, 0), row('a', 130, 300, 3000, 1), row('b', 130, 0), row('b', 160, 900)])).toEqual([
      { ts: 100, objects: 0, bytes: 0, rate: null, byteRate: null },
      { ts: 130, objects: 300, bytes: 3000, rate: 10, byteRate: 100 },
      { ts: 160, objects: 1200, bytes: 12000, rate: 30, byteRate: 300 },
    ])
  })
  it('adds rates of asynchronously sampled parallel buckets', () => {
    expect(historyPoints([row('a', 0, 0), row('b', 0, 0), row('a', 30, 300), row('b', 31, 620)])).toEqual([
      { ts: 0, objects: 0, bytes: 0, rate: null, byteRate: null },
      { ts: 30, objects: 300, bytes: 3000, rate: 10, byteRate: 100 },
      { ts: 31, objects: 920, bytes: 9200, rate: 30, byteRate: 300 },
    ])
  })
  it('does not invent rates across missing samples or counter resets', () => {
    expect(historyPoints([row('a', 0, 20), row('a', 300, 100), row('a', 330, 5), row('a', 360, 35)])).toEqual([
      { ts: 0, objects: 20, bytes: 200, rate: null, byteRate: null },
      { ts: 300, objects: 100, bytes: 1000, rate: null, byteRate: null },
      { ts: 330, objects: 5, bytes: 50, rate: null, byteRate: null },
      { ts: 360, objects: 35, bytes: 350, rate: 1, byteRate: 10 },
    ])
  })
  it('derives byte throughput independently and does not bridge a byte counter reset', () => {
    expect(historyPoints([row('a', 0, 0, 0), row('a', 30, 300, 6000), row('a', 60, 600, 1000), row('a', 90, 900, 4000)])).toEqual([
      { ts: 0, objects: 0, bytes: 0, rate: null, byteRate: null },
      { ts: 30, objects: 300, bytes: 6000, rate: 10, byteRate: 200 },
      { ts: 60, objects: 600, bytes: 1000, rate: 10, byteRate: null },
      { ts: 90, objects: 900, bytes: 4000, rate: 10, byteRate: 100 },
    ])
  })
  it('encodes opaque run IDs and provides stable batch fragments', () => {
    expect([runHref('scan-p1/stamp', 'log'), batchAnchor(7), batchAnchor(null)]).toEqual(['/runs/scan-p1%2Fstamp#log', 'batch-7', 'batch-earlier'])
  })
})
