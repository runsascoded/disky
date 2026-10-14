import { beforeEach, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { onRequestGet } from './age-pyramid'

const { openIndex, readPoint } = vi.hoisted(() => ({ openIndex: vi.fn(), readPoint: vi.fn() }))
vi.mock('../_lib/index.js', () => ({ openIndex, readPoint, storeReady: () => true, num: Number }))
vi.mock('../_lib/auth.js', () => ({ requireViewer: async () => ({}) }))
vi.mock('../_lib/scanArg.js', () => ({ scanArg: async () => '2026-10-12T1809' }))

beforeEach(() => {
  vi.resetAllMocks()
  openIndex.mockResolvedValue({})
})

it('revalidates an empty histogram when the age index is published later', async () => {
  const ctx = { request: new Request('https://r2.oa.dev/api/age-pyramid?date=2026-10-12T1809&path=marin-na'), env: {} as Env }
  openIndex.mockRejectedValueOnce(new Error('not synced'))
  const missing = await onRequestGet(ctx)
  expect([missing.status, missing.headers.get('cache-control'), await missing.json()]).toEqual([
    200, 'private, no-cache', { records: [], plan: { outputBin: '1h', tier: '1h', binBudget: 512 } },
  ])
  openIndex.mockResolvedValue({})
  readPoint.mockResolvedValue([
    { path: 'marin-na', depth: 1, binstart: 1791763200000, b: 100, o: 1 },
    { path: 'marin-na', depth: 1, binstart: 1791849600000, b: 200, o: 2 },
  ])
  const published = await onRequestGet(ctx)
  expect([published.status, published.headers.get('cache-control'), await published.json()]).toEqual([
    200, 'private, no-cache', {
      records: [{ dt: 1791763200000, b: 100, o: 1 }, { dt: 1791849600000, b: 200, o: 2 }],
      plan: { outputBin: '1h', tier: '1h', binBudget: 512 },
    },
  ])
})

it('full-history queries retain the first coarse bin when the first object is later that day', async () => {
  const firstDay = Date.parse('2026-06-01T00:00:00Z')
  const lastDay = Date.parse('2026-07-01T00:00:00Z')
  readPoint.mockResolvedValueOnce([
    { binstart: firstDay + 3600000, b: 100, o: 1 },
    { binstart: lastDay + 23 * 3600000, b: 200, o: 2 },
  ]).mockResolvedValueOnce([
    { binstart: firstDay, b: 100, o: 1 },
    { binstart: lastDay, b: 200, o: 2 },
  ])
  const response = await onRequestGet({ request: new Request('https://r2.oa.dev/api/age-pyramid?date=2026-10-12T1809&path=marin-na&bin_budget=40'), env: {} as Env })
  expect(await response.json()).toEqual({
    records: [{ dt: firstDay, b: 100, o: 1 }, { dt: lastDay, b: 200, o: 2 }],
    plan: { outputBin: '1d', tier: '1d', binBudget: 40 },
  })
})

it('a path whose rows all sit in one bin is that bin, at the finest tier — no plan over an empty range', async () => {
  const bin = Date.parse('2026-10-12T18:00:00Z')
  const age = async (qs: string) => {
    const r = await onRequestGet({ request: new Request(`https://r2.oa.dev/api/age-pyramid?date=2026-10-12T1809&path=marin-na${qs}`), env: {} as Env })
    return [r.status, r.status === 200 ? await r.json() : await r.text()]
  }
  const one = [200, { records: [{ dt: bin, b: 100, o: 1 }], plan: { outputBin: '1h', tier: '1h', binBudget: 512 } }]
  const two = [200, { records: [{ dt: bin, b: 100, o: 1 }, { dt: bin, b: 50, o: 3 }], plan: { outputBin: '1h', tier: '1h', binBudget: 512 } }]
  const at = new Date(bin).toISOString()
  expect([
    await (readPoint.mockResolvedValue([{ binstart: bin, b: 100, o: 1 }]), age('')),
    await (readPoint.mockResolvedValue([{ binstart: bin, b: 100, o: 1 }, { binstart: bin, b: 50, o: 3 }]), age('')),
    await age(`&from=${at}&to=${at}`),
    await age(`&from=${new Date(bin + 1).toISOString()}&to=${at}`),
  ]).toEqual([one, two, two, [400, 'bad range: `from` is after `to`']])
})
