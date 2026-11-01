import { describe, expect, it } from 'vitest'
import { capDiffRows, type DiffRow } from './view'

// `capDiffRows`: a diff cut to `top` keeps every expanded parent exact — the leaves it cuts fold into the parent's
// `(other)` (kept, never counted in `top`). hf's case: many over-floor shards under one directory, `top` below their count.
const GB = 1e9
const row = (p: string, d: number, s: DiffRow['s'], a: number, b: number, oa: number, ob: number, x?: true): DiffRow =>
  ({ p, d, k: 'dir', s, a, b, oa, ob, ...(x ? { x } : {}) })

describe('capDiffRows', () => {
  it('folds the cut shards into their parent\'s `(other)`, and makes a root `(other)` when it had none', () => {
    const shards = Array.from({ length: 10 }, (_, i) => row(`m/s${i}`, 2, 'added', 0, (3.5 + i / 10) * GB, 0, 1))
    const rows = [
      row('m', 1, 'changed', 1 * GB, 1 * GB + 39.6 * GB, 5, 16, true),
      ...shards,
      row('m/(other)', 2, 'changed', 1 * GB, 1.1 * GB, 5, 6),
      row('n', 1, 'added', 0, 2 * GB, 0, 1),
      row('u', 1, 'unchanged', 3 * GB, 3 * GB, 1, 1),
    ]
    const { rows: got, cut } = capDiffRows(rows, 3)
    expect(cut).toBe(true)
    expect(got).toEqual([
      row('m', 1, 'changed', 1 * GB, 40.6 * GB, 5, 16, true),
      row('m/(other)', 2, 'changed', 1 * GB, 1.1 * GB + [0, 1, 2, 3, 4, 5, 6].reduce((s, i) => s + (3.5 + i / 10) * GB, 0), 5, 13),
      row('m/s9', 2, 'added', 0, 4.4 * GB, 0, 1),
      row('m/s8', 2, 'added', 0, 4.3 * GB, 0, 1),
      row('m/s7', 2, 'added', 0, 4.2 * GB, 0, 1),
      row('(other)', 1, 'added', 0, 2 * GB, 0, 1),
    ])
    // Exact under `m`: its drawn children and `(other)` sum to its Δ (bytes and objects).
    const under = got.filter(r => r.p.startsWith('m/'))
    expect([under.reduce((s, r) => s + r.b - r.a, 0), under.reduce((s, r) => s + r.ob - r.oa, 0)]).toEqual([39.6 * GB, 11])
  })

  it('cuts nothing under `top`', () => {
    const rows = [row('a', 1, 'added', 0, 5, 0, 1), row('(other)', 1, 'changed', 3, 4, 1, 1)]
    expect(capDiffRows(rows, 10)).toEqual({ rows, cut: false })
  })
})
