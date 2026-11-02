/** The quota-pressure variants (specs/digest-quota-pressure.md) over the `cw-sep` parity fixture: `main` (main-data)
 * 790 of 910 TiB, `hot` (hot-data) 99.5 of 100 TiB at the month's last scan; `more-data` has no quota. */
import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestCfg, type DigestParams, type DigestRows, imageRequest, opText } from './index'
import type { CwRows } from './cw'
import { mark, quotaLine } from './pools'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const load = async () =>
  JSON.parse(await (await fsp()).readFile(new URL('../../../cloud/tests/fixtures/digest/parity/cw-sep.json', import.meta.url).pathname, 'utf8')) as
    { cfg: DigestCfg; month: string; rows: DigestRows }

const links = { plotUrl: null, prevUrl: null }
const TIB = 1024 ** 4

describe('quota pressure', () => {
  it('marks: red at the alert share or the quota, orange within 10 points below', () => {
    expect([mark(79.9, 90), mark(80, 90), mark(89.9, 90), mark(90, 90), mark(100, 100), mark(99, 100)])
      .toEqual(['', ':large_orange_circle: ', ':large_orange_circle: ', ':red_circle: ', ':red_circle: ', ':large_orange_circle: '])
  })

  it('the OP gains a quota line under the headline: most full first, marked, with headroom', async () => {
    const f = await load()
    const at = (p: Partial<DigestParams>) => opText(f.rows, f.month, f.cfg, links, { ...DEFAULT_PARAMS, ...p }).split('\n')
    const off = at({})
    const on = at({ quotas: 'circles' })
    expect(on).toEqual([off[0], '*Quotas:* :red_circle: hot 99.5% of 100Ti (0.5 Ti free) · :large_orange_circle: main 86.8% of 1P (120.0 Ti free)', ...off.slice(1)])
    expect(at({ quotas: 'circles', alertAt: 100 })[1]).toBe('*Quotas:* :large_orange_circle: hot 99.5% of 100Ti (0.5 Ti free) · main 86.8% of 1P (120.0 Ti free)')
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
    expect(quotaLine({ ...last, tb: 100 }, zoned, 90)).toBe('*Quotas:* :red_circle: hot 101.0% of 100Ti (1.0 Ti over) · z zone 11%')
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
