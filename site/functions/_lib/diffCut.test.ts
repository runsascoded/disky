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
    const { rows: got, cut } = capDiffRows(rows, 3, { a: 4 * GB, b: 45.6 * GB, oa: 6, ob: 18 })
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
    expect(capDiffRows(rows, 10, { a: 3, b: 9, oa: 1, ob: 2 })).toEqual({ rows, cut: false })
  })
})

describe('capDiffRows from the invariant', () => {
  it('derives `(other)` from the parent less its drawn children, whatever left children out (a lookup budget)', () => {
    // Snowball's shape: +268.3 GB, 26 children read (+91.2 GB), the rest never read (no row, so nothing to fold).
    const kids = Array.from({ length: 26 }, (_, i) => row(`s/c${String(i).padStart(2, '0')}`, 2, 'added', 0, 91.2 * GB / 26, 0, 1))
    const { rows } = capDiffRows([row('s', 1, 'changed', 10 * GB, 278.3 * GB, 5, 100, true), ...kids], 1e6, { a: 10 * GB, b: 278.3 * GB, oa: 5, ob: 100 })
    const under = rows.filter(r => r.p.startsWith('s/'))
    expect(rows.find(r => r.p === 's/(other)')).toEqual(row('s/(other)', 2, 'changed', 10 * GB, 278.3 * GB - 91.2 * GB, 5, 74))
    expect(under.reduce((x, r) => x + r.b - r.a, 0)).toBeCloseTo(268.3 * GB, -3)
  })
})

describe('capDiffRows at the root', () => {
  it('folds a cut child of the root into a root `(other)` (hf: open-athena/models, one tiny child cut)', () => {
    const kids = [row('a', 1, 'changed', 100, 200, 3, 4), row('b', 1, 'changed', 50, 60, 2, 2), row('t', 1, 'changed', 1000, 1001, 4, 4)]
    const root = { a: 1150, b: 1261, oa: 9, ob: 10 }
    const { rows, cut } = capDiffRows(kids, 2, root)
    expect(cut).toBe(true)
    expect(rows).toEqual([row('a', 1, 'changed', 100, 200, 3, 4), row('b', 1, 'changed', 50, 60, 2, 2), row('(other)', 1, 'changed', 1000, 1001, 4, 4)])
    const sum = (f: (r: DiffRow) => number) => rows.reduce((s, r) => s + f(r), 0)
    expect([sum(r => r.b - r.a), sum(r => r.ob - r.oa)]).toEqual([root.b - root.a, root.ob - root.oa])
  })
})

describe('capDiffRows: zero-byte objects', () => {
  it('keeps an objects-only `(other)` (gcs: icd-warmstart-tpu-001, 19 zero-byte files beside `cells`)', () => {
    const rows = [row('k', 1, 'changed', 2353104154, 3765485860, 24, 165, true), row('k/cells', 2, 'changed', 2353104154, 3765485860, 24, 146)]
    const { rows: got } = capDiffRows(rows, 1e6, { a: 2353104154, b: 3765485860, oa: 24, ob: 165 })
    expect(got.filter(r => r.p.startsWith('k/'))).toEqual([row('k/cells', 2, 'changed', 2353104154, 3765485860, 24, 146), row('k/(other)', 2, 'added', 0, 0, 0, 19)])
  })
})
