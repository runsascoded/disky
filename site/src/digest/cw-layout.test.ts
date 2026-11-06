/** `layout: 'pools'`: the OP's headline and weeks over every bucket, a line per pool. */
import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestCfg, opText } from './index'
import type { CwRows, CwScan } from './cw'

const TIB = 1024 ** 4
const quotas = [{ bytes: 100 * TIB, name: '100 TiB', short: '100Ti', classes: [] }]
const cfg: DigestCfg = {
  template: 'cw', title: 'T', site_url: 'https://x.test', host: 'x.test', unit: { name: 'TiB', bytes: TIB, short: 'Ti' }, primary: 'main', prices: {}, icons_base: '',
  buckets: {
    main: { label: 'main', path: null, zone: null, quotas },
    a: { label: 'a', path: null, zone: 'z', quotas, shape: 'star' },
    b: { label: 'b', path: null, zone: 'z', quotas, shape: 'star' },
    still: { label: 'still', path: null, zone: null, quotas, shape: 'diamond' },
  },
}
const scan = (s: string, tb: number, extra: Record<string, number>): CwScan => ({ scan: s, slug: s.slice(5, 10).replace('-', ''), tb, extra, classes: null })
const rows: CwRows = {
  template: 'cw', closed: false, pre: [],
  lead: [scan('2026-08-31T1200', 50, { a: 10, still: 5 })],
  rows: [scan('2026-09-07T1200', 60, { a: 15, still: 5 }), scan('2026-09-14T1200', 58, { a: 15.5, b: 20, still: 5 })],
}
const text = (quotas: 'runway-hue' | 'off') =>
  opText(rows, '2026-09', cfg, { plotUrl: null, prevUrl: null }, { ...DEFAULT_PARAMS, layout: 'pools', quotas }).replace(/\]\([^)]*\)/g, ']').split('\n')

const WEEKS = [
  '*Weekly changes*',
  ':arrow_deg60: [wk of 9/7]: **+15.0 TiB** · main +10.0 · z zone +5.0',
  ':arrow_deg-30: [wk of 9/14] _(partial)_: **−1.5 TiB** · main −2.0 · new: b 20',
]

describe("the OP's per-pool layout", () => {
  it("totals every bucket, a bucket's first scan counting as new rather than growth", () => {
    expect(text('runway-hue')).toEqual([
      ':arrow_deg50: **+13.5 TiB** [month-to-date] · 98 TiB total · [dashboard]',
      '_as of 9/14 12:00Z_',
      '',
      '*Buckets* (Δ month-to-date)',
      ':runway-8: [main] 58 TiB (+8.0) · 58.0% of 100Ti (42.0 Ti free)',
      ':runway-star-6: z zone 36 TiB (+5.5): [a] 16, [b] 20 · 35.5% of 100Ti (64.5 Ti free), +2.9 Ti/d, ≈22d to full',
      ':runway-diamond-8: [still] 5 TiB · 5%',
      '',
      ...WEEKS,
    ])
  })
  it('keeps the lines without marks when the quota marks are off', () => {
    expect(text('off').slice(4, 7)).toEqual([
      '[main] 58 TiB (+8.0) · 58.0% of 100Ti (42.0 Ti free)',
      'z zone 36 TiB (+5.5): [a] 16, [b] 20 · 35.5% of 100Ti (64.5 Ti free), +2.9 Ti/d, ≈22d to full',
      '[still] 5 TiB · 5%',
    ])
  })
})
