import { describe, expect, it } from 'vitest'
import { checkpointReceipt, compactHistory } from './checkpoint'

const checkpoint = 'gs://own-data/access/r2/closed.parquet'
const source = 'r2://own-archive/state.json'
const cursor = Date.parse('2026-10-12T08:00Z') / 1000
const valid = () => ({ schema_version: 1, kind: 'r2-read-checkpoint', source, account: 'own',
  checkpoint: { uri: checkpoint, generation: '17', sha256: 'a'.repeat(64) }, cursors: { b: cursor } })

describe('durable checkpoint receipts', () => {
  it('requires the intended account, source, durable object and revision proof', () => {
    const r = valid()
    expect(checkpointReceipt(r, 'own', source, checkpoint)).toEqual(r)
    const cases: unknown[] = [null, [], {}, { ...r, schema_version: 2 }, { ...r, account: 'other' },
      { ...r, source: 'r2://other/state.json' }, { ...r, checkpoint: { ...r.checkpoint, uri: 'gs://other/closed' } },
      { ...r, checkpoint: { ...r.checkpoint, generation: null } }, { ...r, checkpoint: { ...r.checkpoint, sha256: 'bad' } },
      { ...r, cursors: {} }, { ...r, cursors: { b: -1 } }, { ...r, cursors: { b: NaN } },
      { ...r, cursors: { b: '17' } }]
    expect(cases.map(value => checkpointReceipt(value, 'own', source, checkpoint))).toEqual(cases.map(() => null))
  })

  it('compacts only frozen windows covered by the receipt and records omitted history', () => {
    const buckets = { b: { frozen: { '2026-10-12T07': 'a', '2026-10-12T08:00': 'b', '2026-10-12T08:05': 'c' } },
      noProof: { frozen: { '2026-10-12T07': 'd' } },
      constructor: { frozen: { '2026-10-12T07': 'unproved' } },
      legacy: { frozen: { '2026-10-12T07': 'e' }, legacy_days: { '2026-10-01': 'old-day' } } }
    const r = checkpointReceipt(valid(), 'own', source, checkpoint)!
    r.cursors.legacy = cursor
    expect(compactHistory(buckets, r)).toBe(2)
    expect(buckets).toEqual({ b: { frozen: { '2026-10-12T08:05': 'c' }, closed: { through: cursor, checkpoint } },
      noProof: { frozen: { '2026-10-12T07': 'd' } },
      constructor: { frozen: { '2026-10-12T07': 'unproved' } },
      legacy: { frozen: { '2026-10-12T07': 'e' }, legacy_days: { '2026-10-01': 'old-day' } } })
  })

  it('never moves a saved bound backwards or adopts a different checkpoint', () => {
    const r = checkpointReceipt(valid(), 'own', source, checkpoint)!
    const buckets = { b: { frozen: { '2026-10-12T07': 'a' }, closed: { through: cursor + 300, checkpoint } } }
    const before = structuredClone(buckets)
    expect(compactHistory(buckets, r)).toBe(0)
    expect(buckets).toEqual(before)
    buckets.b.closed = { through: cursor, checkpoint: 'gs://other/closed' }
    const other = structuredClone(buckets)
    expect(compactHistory(buckets, r)).toBe(0)
    expect(buckets).toEqual(other)
  })
})
