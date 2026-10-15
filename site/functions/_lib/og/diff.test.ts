import { beforeEach, describe, expect, it, vi } from 'vitest'
import { diffCardData, resolveDiffScans } from './diff'
import type { Diff } from '../view'
import { mint, fullTier } from './tokens'
import { pageView } from './routes'
import { sqliteD1 } from '../testD1'

const mocks = vi.hoisted(() => ({ scans: vi.fn() }))
vi.mock('../index', async importOriginal => ({ ...await importOriginal<typeof import('../index')>(), pathScans: mocks.scans }))
vi.mock('../scanTimes', () => ({ scanTimes: async () => ({}) }))
beforeEach(() => mocks.scans.mockResolvedValue({ results: ['2026-09-17T0000', '2026-09-28T1200', '2026-09-30T1200'].map(date => ({ date })) }))

describe('D1', () => {
  const syntheticDiff: Diff = {
    total_a: 100, total_b: 110, objects_a: 10, objects_b: 11, expansions: 0,
    truncated: false, threshold: 1, tier: 'path', lookups: 0, lookups_capped: false,
    rows: [
      { p: 'grew', d: 1, k: 'dir', s: 'changed', a: 40, b: 70, oa: 4, ob: 7 },
      { p: 'shrank', d: 1, k: 'dir', s: 'changed', a: 60, b: 40, oa: 6, ob: 4 },
    ],
  }

  it('resolves both endpoints exactly, with default previous scan and no miss fallback', async () => {
    expect(await Promise.all(['26092812-26091700', undefined, '-1d', '26091700'].map(d => resolveDiffScans({}, d))))
      .toEqual([
        { from: '2026-09-17T0000', to: '2026-09-28T1200' },
        { from: '2026-09-28T1200', to: '2026-09-30T1200' },
        { from: '2026-09-28T1200', to: '2026-09-30T1200' },
        { from: null, to: '2026-09-17T0000' },
      ])
    await expect(resolveDiffScans({}, '26092812-26091800')).rejects.toThrow('no scan matches d=26092812-26091800')
  })

  it('labels signed changes independently of absolute movement weights', () => {
    expect(diffCardData({ name: 'Demo', scheme: 's3://' }, 'bucket · changes', { path: 'bucket' }, 'full', 'before', 'after', syntheticDiff))
      .toEqual({
        tier: 'full', site: 'Demo', title: 'bucket · changes', subtitle: 'before → after · area |Δ|',
        total: '100 B → 110 B · net +10 B', empty: 'no changes',
        tiles: [
          { name: 'grew', b: 30, value: '+30 B', color: 'rgba(63, 185, 80, 0.514)' },
          { name: 'shrank', b: 20, value: '−20 B', color: 'rgba(248, 81, 73, 0.433)' },
        ],
      })
  })

  it('minting a fragment-only diff link scopes it to the comparison, not its snapshot map', async () => {
    const { db } = await sqliteD1()
    const now = Date.UTC(2026, 9, 13) / 1000
    const result = await mint(db, 'Demo', new URL('https://demo.example/bucket?d=26092812-26091700#diff'), 'demo', now, 30, 'DiffTokAAA')
    expect(result?.url).toBe('https://demo.example/bucket?d=26092812-26091700&section=diff&og=DiffTokAAA#diff')
    const view = pageView(new URL(result!.url), 'Demo')!
    expect([view.kind, await fullTier(db, 'diff', view.params, result!.token, now), await fullTier(db, 'map', view.params, result!.token, now)])
      .toEqual(['diff', { day: result!.expDay }, null])
  })
})
