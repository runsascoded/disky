import { describe, expect, it } from 'vitest'
import type { PerfEntry } from '../src/perf.ts'
import { aggregate, diffAggs, fmtTable, quantile, renderAggs, renderDiff, type Agg, type RunResult } from './lib.ts'

const entry = (o: Partial<PerfEntry> & Pick<PerfEntry, 'widget' | 'key'>): PerfEntry => ({
  id: 0, start: 0, wait: null, decode: null, paint: null, settle: null, total: null, server: {}, cache: null, status: 200, done: true, failed: false, empty: false, ...o,
})
const run = (n: number, entries: PerfEntry[]): RunResult => ({ path: '/', run: n, ms: 0, timedOut: false, open: [], entries })

describe('quantile', () => {
  it('interpolates linearly between order statistics', () => {
    expect(quantile([3, 1, 2], 0.5)).toBe(2)
    expect(quantile([1, 2, 3, 4], 0.5)).toBe(2.5)
    expect(quantile([10, 20, 30], 0.95)).toBe(29)
    expect(quantile([7], 0.95)).toBe(7)
    expect(quantile([], 0.5)).toBeNaN()
  })
})

describe('aggregate', () => {
  it('folds loads by (widget, key) over runs: p50/p95 per phase, server phases, cache tiers, failures', () => {
    const results = [
      run(1, [
        entry({ widget: 'treemap', key: '/@d|w1280', wait: 100, decode: 4, paint: 10, settle: 0, total: 114, server: { fetch: 80, total: 90 }, cache: 'miss' }),
        entry({ widget: 'table', key: '/@d|w1280', wait: 100, decode: 4, paint: 30, settle: 0, total: 134, server: { fetch: 80, total: 90 }, cache: 'miss' }),
        entry({ widget: 'series', key: '/|n3', wait: 50, decode: 1, paint: null, settle: null, total: null, done: false, failed: true }),
      ]),
      run(2, [
        entry({ widget: 'treemap', key: '/@d|w1280', wait: 20, decode: 2, paint: 10, settle: 0, total: 32, server: { fetch: 0, total: 4 }, cache: 'hit' }),
        entry({ widget: 'table', key: '/@d|w1280', wait: 20, decode: 2, paint: 20, settle: 0, total: 42, server: { fetch: 0, total: 4 }, cache: 'hit' }),
      ]),
      run(3, [
        entry({ widget: 'treemap', key: '/@d|w1280', wait: 30, decode: 3, paint: 12, settle: 100, total: 145, server: { fetch: 0, total: 5 }, cache: 'hit' }),
      ]),
    ]
    const expected: Agg[] = [
      {
        widget: 'treemap', key: '/@d|w1280', n: 3,
        phases: { wait: { p50: 30, p95: 93, n: 3 }, decode: { p50: 3, p95: 3.9, n: 3 }, paint: { p50: 10, p95: 11.8, n: 3 }, settle: { p50: 0, p95: 90, n: 3 }, total: { p50: 114, p95: 141.9, n: 3 } },
        server: { fetch: { p50: 0, p95: 72, n: 3 }, total: { p50: 5, p95: 81.5, n: 3 } },
        cache: { miss: 1, hit: 2 }, failed: 0,
      },
      {
        widget: 'table', key: '/@d|w1280', n: 2,
        phases: { wait: { p50: 60, p95: 96, n: 2 }, decode: { p50: 3, p95: 3.9, n: 2 }, paint: { p50: 25, p95: 29.5, n: 2 }, settle: { p50: 0, p95: 0, n: 2 }, total: { p50: 88, p95: 129.4, n: 2 } },
        server: { fetch: { p50: 40, p95: 76, n: 2 }, total: { p50: 47, p95: 85.7, n: 2 } },
        cache: { miss: 1, hit: 1 }, failed: 0,
      },
      {
        widget: 'series', key: '/|n3', n: 1,
        phases: { wait: { p50: 50, p95: 50, n: 1 }, decode: { p50: 1, p95: 1, n: 1 } },
        server: {}, cache: {}, failed: 1,
      },
    ]
    expect(aggregate(results)).toEqual(expected)
  })
})

describe('fmtTable', () => {
  it('pads columns, right-aligns where asked, never leaves trailing spaces', () => {
    expect(fmtTable([['key', 'n', 'wait'], ['/ctbk', '3', '210/240'], ['/', '12', '5/6']], ['l', 'r', 'r'])).toBe([
      'key     n     wait',
      '/ctbk   3  210/240',
      '/      12      5/6',
    ].join('\n'))
  })
})

describe('renderAggs / diffAggs / renderDiff', () => {
  const a: Agg[] = [
    { widget: 'treemap', key: 'k', n: 2, phases: { wait: { p50: 100, p95: 120, n: 2 }, total: { p50: 150, p95: 160, n: 2 } }, server: { fetch: { p50: 80, p95: 90, n: 2 } }, cache: { hit: 2 }, failed: 0 },
    { widget: 'age', key: 'x', n: 1, phases: { wait: { p50: 40, p95: 40, n: 1 } }, server: {}, cache: {}, failed: 1 },
  ]
  const b: Agg[] = [
    { widget: 'treemap', key: 'k', n: 2, phases: { wait: { p50: 50, p95: 70, n: 2 }, total: { p50: 90, p95: 100, n: 2 }, paint: { p50: 8, p95: 9, n: 2 } }, server: { fetch: { p50: 20, p95: 30, n: 2 } }, cache: { hit: 2 }, failed: 0 },
    { widget: 'series', key: 's', n: 1, phases: { wait: { p50: 5, p95: 5, n: 1 } }, server: {}, cache: {}, failed: 0 },
  ]
  it('renders one block per widget in widget order', () => {
    expect(renderAggs(a).split('\n')).toEqual([
      '## treemap',
      'key  n  wait p50/p95  decode p50/p95  paint p50/p95  settle p50/p95  total p50/p95  cache  server p50',
      'k    2       100/120               –              –               –        150/160  hit×2  fetch 80',
      '',
      '## age',
      'key             n  wait p50/p95  decode p50/p95  paint p50/p95  settle p50/p95  total p50/p95  cache  server p50',
      'x    1 (1 failed)         40/40               –              –               –              –',
    ])
  })
  it('diffs p50s per phase, keeping one-sided rows with a null side', () => {
    expect(diffAggs(a, b)).toEqual([
      { widget: 'treemap', key: 'k', phase: 'wait', a: 100, b: 50, d: -50, pct: -50 },
      { widget: 'treemap', key: 'k', phase: 'paint', a: null, b: 8, d: null, pct: null },
      { widget: 'treemap', key: 'k', phase: 'total', a: 150, b: 90, d: -60, pct: -40 },
      { widget: 'treemap', key: 'k', phase: 'server:fetch', a: 80, b: 20, d: -60, pct: -75 },
      { widget: 'series', key: 's', phase: 'wait', a: null, b: 5, d: null, pct: null },
      { widget: 'age', key: 'x', phase: 'wait', a: 40, b: null, d: null, pct: null },
    ])
    expect(renderDiff(diffAggs(a, b)).split('\n')).toEqual([
      '## treemap',
      'key  phase         a p50  b p50  Δ ms   Δ %',
      'k    wait            100     50   -50  -50%',
      'k    paint             –      8     –     –',
      'k    total           150     90   -60  -40%',
      'k    server:fetch     80     20   -60  -75%',
      '',
      '## series',
      'key  phase  a p50  b p50  Δ ms  Δ %',
      's    wait       –      5     –    –',
      '',
      '## age',
      'key  phase  a p50  b p50  Δ ms  Δ %',
      'x    wait      40      –     –    –',
    ])
  })
})
