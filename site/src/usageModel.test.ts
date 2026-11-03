// /admin/usage's model over two synthetic days in `cf_usage.snapshot`'s shape: the window's totals against the plan
// (pro rata, storage as a mean level), product costs per day, attribution folded to top N, and coverage.
import { describe, expect, it } from 'vitest'
import { attribution, coverage, fmtCount, fmtDollars, lastDays, productCosts, type UsageSnapshot, type UsageSums, windowTotals } from './usageModel'

const PLAN = {
  functions_requests: { label: 'Functions requests', usd: 0.3, per: 1e6, included: 10e6, storage: false },
  r2_storage_gb: { label: 'R2 storage GB-mo', usd: 0.015, per: 1, included: 10, storage: true },
  d1_rows_written: { label: 'D1 rows written', usd: 1, per: 1e6, included: 50e6, storage: false },
}
const sums = (requests: number, d1_rows_written = 0, r2_reads = 0): UsageSums =>
  ({ requests, wall_ms: requests * 10, r2_reads, r2_bytes: r2_reads * 1000, d1_queries: requests, d1_rows_read: 0, kv_reads: 0, d1_rows_written })

const day = (date: string, units: Record<string, number>, usd: Record<string, number>, extra: Partial<UsageSnapshot> = {}): UsageSnapshot => ({
  version: 1, date, project: 'p', units, usd, plan: PLAN, functions: {}, by_caller: [], by_route: [], notes: [], ...extra,
})

const D1 = day('2026-10-19', { functions_requests: 1000, r2_storage_gb: 300, d1_rows_written: 2e6 }, { functions_requests: 0.0003, r2_storage_gb: 0.15, d1_rows_written: 2 }, {
  by_caller: [
    { caller: 'session', via: 'session', ...sums(500, 10) },
    { caller: 'sheet sync', via: 'token', ...sums(100, 0, 900) },
    { caller: '(other)', via: '', ...sums(4) },
  ],
  by_route: [{ route: '/api/subtree', ...sums(300, 0, 900) }, { route: 'page', ...sums(304) }],
})
const D2 = day('2026-10-20', { functions_requests: 3000, r2_storage_gb: 500, d1_rows_written: 1e6 }, { functions_requests: 0.0009, r2_storage_gb: 0.25, d1_rows_written: 1 }, {
  by_caller: [
    { caller: 'session', via: 'session', ...sums(200, 5) },
    { caller: 'bot', via: 'bot', ...sums(50) },
    { caller: 'agent', via: 'token', ...sums(20, 1000) },
  ],
})

describe('usage model', () => {
  it('takes the newest days, oldest first', () => {
    expect(lastDays([D2, D1], 1).map(s => s.date)).toEqual(['2026-10-20'])
    expect(lastDays([D2, D1], 7).map(s => s.date)).toEqual(['2026-10-19', '2026-10-20'])
  })

  it('totals the window against the plan pro rata', () => {
    const t = windowTotals([D1, D2])
    expect(t.map(({ unit, total, included, over, listUsd }) => [unit, total, included, over, listUsd])).toEqual([
      ['functions_requests', 4000, 10e6 * 2 / 30, 0, 0.0012],
      // storage: the mean level (400 GB) against 10 GB, billed as GB-months over 2/30 of a month
      ['r2_storage_gb', 400, 10 * 2 / 30, 400 - 10 * 2 / 30, 0.4],
      ['d1_rows_written', 3e6, 50e6 * 2 / 30, 0, 3],
    ])
    expect(t.map(x => x.overUsd)).toEqual([0, (400 - 10 * 2 / 30) * 0.015 * 2 / 30, 0])
  })

  it('sums each product\'s units per day', () => {
    expect(productCosts(D1)).toEqual({ d1w: 2, r2s: 0.15, d1r: 0, r2o: 0, fn: 0.0003, kv: 0 })
  })

  it('folds attribution past top N, and each day\'s (other), into one (other) last', () => {
    expect(attribution([D1, D2], 'caller', 2).map(r => [r.name, r.via, r.requests, r.d1_rows_written, r.r2_reads])).toEqual([
      ['session', 'session', 700, 15, 0],
      ['sheet sync', 'token', 100, 0, 900],
      ['(other)', '', 74, 1000, 0],
    ])
    expect(attribution([D1, D2], 'caller', 3, 'd1_rows_written').map(r => r.name)).toEqual(['agent', 'session', 'bot', '(other)'])
    expect(attribution([D1, D2], 'route', 5).map(r => [r.name, r.requests])).toEqual([['page', 304], ['/api/subtree', 300]])
  })

  it('reports how many billed requests carried a point', () => {
    expect(coverage([D1, D2])).toEqual({ billed: 4000, attributed: 874 })
  })

  it('formats counts and dollars', () => {
    expect([0, 12, 1234, 25_600_000, 3.21e9].map(fmtCount)).toEqual(['0', '12', '1.23K', '25.6M', '3.21G'])
    expect([0.004, 1.5, 99.999, 1234.4].map(fmtDollars)).toEqual(['$0.00', '$1.50', '$100.00', '$1,234'])
  })
})
