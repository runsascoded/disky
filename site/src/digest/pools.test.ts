/** The quota-pressure variants (specs/digest-quota-pressure.md) over the `cw-sep` parity fixture: `main` (main-data)
 * 790 of 910 TiB, `hot` (hot-data) 99.5 of 100 TiB at the month's last scan; `more-data` has no quota. */
import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestCfg, type DigestParams, type DigestRows, imageRequest, opText } from './index'
import type { CwRows, CwScan } from './cw'
import { growth, mark, quotaLine, runway, runwayHue, runwayMark, runwayText } from './pools'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const load = async () =>
  JSON.parse(await (await fsp()).readFile(new URL('../../../cloud/tests/fixtures/digest/parity/cw-sep.json', import.meta.url).pathname, 'utf8')) as
    { cfg: DigestCfg; month: string; rows: DigestRows }

const links = { plotUrl: null, prevUrl: null }
const TIB = 1024 ** 4

describe('quota pressure', () => {
  it('marks: red at the alert share or the quota, orange within 10 points below', () => {
    expect([mark(49.9, 90), mark(50, 90), mark(79.9, 90), mark(80, 90), mark(89.9, 90), mark(90, 90), mark(100, 100), mark(99, 100)]).toEqual([
      ':large_green_circle: ', ':large_yellow_circle: ', ':large_yellow_circle: ', ':large_orange_circle: ', ':large_orange_circle: ', ':red_circle: ',
      ':red_circle: ', ':large_orange_circle: ',
    ])
  })

  it('the OP gains a quota line under the headline: most full first, marked, with headroom', async () => {
    const f = await load()
    const at = (p: Partial<DigestParams>) => opText(f.rows, f.month, f.cfg, links, { ...DEFAULT_PARAMS, ...p }).split('\n')
    const off = at({})
    const on = at({ quotas: 'circles' })
    expect(on).toEqual([off[0], '*Quotas:* :red_circle: hot 99.5% of 100Ti (0.5 Ti free) · :large_orange_circle: main 86.8% of 1P (120.0 Ti free)', ...off.slice(1)])
    expect(at({ quotas: 'circles', alertAt: 100 })[1]).toBe('*Quotas:* :large_orange_circle: hot 99.5% of 100Ti (0.5 Ti free) · :large_yellow_circle: main 86.8% of 1P (120.0 Ti free)')
  })

  it('a zone sums its buckets against its quota; an overage reads "over"; a quiet pool is a bare share', async () => {
    const f = await load()
    const rows = f.rows as CwRows
    const last = { ...rows.rows[rows.rows.length - 1], extra: { 'hot-data': 101, 'more-data': 2.4 } }
    const zoned: DigestCfg = {
      ...f.cfg,
      buckets: {
        ...f.cfg.buckets,
        'main-data': { ...f.cfg.buckets['main-data'], zone: 'z' },
        'more-data': { label: 'more', path: null, zone: 'z', quotas: f.cfg.buckets['main-data'].quotas },
        'cold-data': { label: 'cold', path: null, zone: null, quotas: [{ bytes: 100 * TIB, name: '100 TiB', short: '100Ti', classes: [] }] },
      },
    }
    // z = main 790 + more 2.4 of 910; cold isn't in the scan (no clause)
    expect(quotaLine(last, zoned, 90)).toBe('*Quotas:* :red_circle: hot 101.0% of 100Ti (1.0 Ti over) · :large_orange_circle: z zone 87.1% of 1P (117.6 Ti free)')
    expect(quotaLine({ ...last, tb: 100 }, zoned, 90)).toBe('*Quotas:* :red_circle: hot 101.0% of 100Ti (1.0 Ti over) · :large_green_circle: z zone 11%')
  })

  it('runway: the fastest fill over 1, 3 and 7 days; marks by runway, ≥ 95% never calmer than orange (digest_cw)', async () => {
    const f = await load()
    const D = 86400_000
    const t = Date.UTC(2026, 8, 23, 12)
    expect(growth([[t - 7 * D, 80], [t - 3 * D, 80], [t - D, 80]], t, 90)).toBe(10)
    expect(growth([[t - 7 * D, 46], [t - 3 * D, 77], [t - D, 83]], t, 85)).toBe(39 / 7)
    expect(growth([[t - D / 2, 80]], t, 90)).toBe(null)
    expect([runway(15, 5), runway(15, 0), runway(15, -2), runway(15, null), runway(-1.3, 4)]).toEqual([
      { days: 3, growth: 5 }, { days: Infinity, growth: 0 }, { days: Infinity, growth: -2 }, { days: Infinity, growth: null }, { days: 0, growth: 4 },
    ])
    const [R, O, Y, G] = [':red_circle: ', ':large_orange_circle: ', ':large_yellow_circle: ', ':large_green_circle: ']
    const marks = ([[0, 101], [2.9, 50], [3, 50], [13.9, 50], [14, 50], [14, 95], [29.9, 50], [30, 50], [Infinity, 88.5], [Infinity, 96]] as [number, number][])
      .map(([days, pct]) => runwayMark({ days, growth: 1 }, pct))
    expect(marks).toEqual([R, R, O, O, Y, O, Y, G, G, O])
    expect([{ days: 8.2, growth: 4.1 }, { days: 0.5, growth: 30 }, { days: 59.9, growth: 1 }, { days: 60, growth: 1 }, { days: Infinity, growth: 0 }, { days: 0, growth: 4 }]
      .map(r => runwayText(r, f.cfg))).toEqual([', +4.1 Ti/d, ≈8d to full', ', +30.0 Ti/d, ≈<1d to full', ', +1.0 Ti/d, ≈60d to full', '', '', ''])
  })

  it('runway-hue: :runway-N: one more than the cut points reached; ≥ 95% never calmer than 3 (digest_cw._runway_hue)', () => {
    const days = [0, 1.79, 1.8, 2.9, 3, 5.9, 6, 9.9, 10, 18.9, 19, 32.9, 33, 59.9, 60, Infinity]
    expect(days.map(d => runwayHue({ days: d, growth: 1 }, 50))).toEqual([1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8].map(n => `:runway-${n}: `))
    expect(([[Infinity, 94.9], [Infinity, 95], [2, 99], [0, 101]] as [number, number][]).map(([d, pct]) => runwayHue({ days: d, growth: 1 }, pct)))
      .toEqual([':runway-8: ', ':runway-3: ', ':runway-2: ', ':runway-1: '])
  })

  it('the runway quota line looks back into the previous month: day 2 of November sees October’s 7-day fill', async () => {
    const f = await load()
    const s = (scan: string, hot: number): CwScan => ({ scan, slug: null, tb: 790, extra: { 'hot-data': hot }, classes: null })
    const m: CwRows = { template: 'cw', closed: false, pre: [s('2026-10-26T1201', 42)], lead: [s('2026-10-31T1201', 81.8)], rows: [s('2026-11-01T1201', 81.9), s('2026-11-02T1201', 82)] }
    const line = (rows: CwRows) => opText(rows, '2026-11' as typeof f.month, f.cfg, links, { ...DEFAULT_PARAMS, quotas: 'runway' }).split('\n')[1]
    // +0.1 Ti/d within November, but 42 → 82 over the week before: orange by the 7-day rate, 18 Ti free ≈ 3 days
    expect(line(m)).toBe('*Quotas:* :large_green_circle: main 86.8% of 1P (120.0 Ti free) · :large_orange_circle: hot 82.0% of 100Ti (18.0 Ti free), +5.7 Ti/d, ≈3d to full')
    // without the previous month's scans it would read calm
    expect(line({ ...m, pre: [] })).toBe('*Quotas:* :large_green_circle: main 86.8% of 1P (120.0 Ti free) · :large_green_circle: hot 82.0% of 100Ti (18.0 Ti free)')
  })

  it('the runway quota line: the replies’ order, a flat full pool green, a filling one red with its rate', async () => {
    const f = await load()
    const s = (scan: string, hot: number): CwScan => ({ scan, slug: null, tb: 790, extra: { 'hot-data': hot }, classes: null })
    const seen = [s('2026-09-01T0001', 50), s('2026-09-05T0001', 80), s('2026-09-07T0001', 90), s('2026-09-08T0001', 99.5)]
    const last = seen[seen.length - 1]
    // main flat at 86.8%: never filling, green; hot +9.5 Ti over the last day with 0.5 Ti free: red, under a day
    expect(quotaLine(last, f.cfg, 90, 'runway', seen)).toBe(
      '*Quotas:* :large_green_circle: main 86.8% of 1P (120.0 Ti free) · :red_circle: hot 99.5% of 100Ti (0.5 Ti free), +9.5 Ti/d, ≈<1d to full')
    // no history: nothing filling; hot ≥ 95% stays orange
    expect(quotaLine(last, f.cfg, 90, 'runway', [last])).toBe(
      '*Quotas:* :large_green_circle: main 86.8% of 1P (120.0 Ti free) · :large_orange_circle: hot 99.5% of 100Ti (0.5 Ti free)')
    // `runway-hue`: the same order and text, as `:runway-N:` steps
    expect(quotaLine(last, f.cfg, 90, 'runway-hue', seen)).toBe(
      '*Quotas:* :runway-8: main 86.8% of 1P (120.0 Ti free) · :runway-1: hot 99.5% of 100Ti (0.5 Ti free), +9.5 Ti/d, ≈<1d to full')
    // `circles` ignores the history: most full first, by share
    expect(quotaLine(last, f.cfg, 90, 'circles', seen)).toBe(
      '*Quotas:* :red_circle: hot 99.5% of 100Ti (0.5 Ti free) · :large_orange_circle: main 86.8% of 1P (120.0 Ti free)')
  })

  it('the pools plot: every pool’s used bytes per scan against its quota, the alert share, the same diff', async () => {
    const f = await load()
    const usage = imageRequest(f.rows, f.month, f.cfg, DEFAULT_PARAMS)
    const req = imageRequest(f.rows, f.month, f.cfg, { ...DEFAULT_PARAMS, plot: 'pools', alertAt: 85 })
    const pools = req.pools as { name: string; quota: number; points: { scan: string; bytes: number }[] }[]
    expect([req.kind, req.alert_pct, req.diff, pools.map(p => [p.name, p.quota, p.points[p.points.length - 1]])]).toEqual([
      'pools', 85, usage.diff,
      [['main', 910 * TIB, { scan: '2026-09-08T0001', bytes: 790 * TIB }], ['hot', 100 * TIB, { scan: '2026-09-08T0001', bytes: 99.5 * TIB }]],
    ])
    expect(pools[0].points.map(p => p.scan)).toEqual((usage.points as { scan: string }[]).map(p => p.scan))
    expect('kind' in usage).toBe(false)
  })
})
