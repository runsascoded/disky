import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mapCard } from './data'
import type { TreeNode } from '../../../src/types'

const mocks = vi.hoisted(() => ({ tree: vi.fn(), meta: vi.fn() }))
vi.mock('../index', () => ({
  pathScans: async () => ({ results: [{ date: '2026-10-13T1000' }] }),
  servedStore: () => ({ get: mocks.meta }),
}))
vi.mock('../scanTimes', () => ({ scanTimes: async () => ({}) }))
vi.mock('../identity', () => ({ loadRegistry: async () => ({}), canonId: (s: string) => s }))
vi.mock('../staticFilter', () => ({ hexQuery: async (_env: unknown, query: unknown) => ({ query }) }))
vi.mock('../view', () => ({ buildView: mocks.tree, ATTEN_DEFAULT: 0.5, MIN_AREA_DEFAULT: 20, NotFound: class extends Error {} }))

const tree: TreeNode = {
  n: 'root', b: 100, o: 10, us: [['ann', 50]],
  c: [
    { n: 'a', b: 60, o: 6, us: [['ann', 50]], c: [
      { n: 'a1', b: 50, o: 5, d: 10, a: 10, us: [['ann', 50]] },
      { n: 'a2', b: 10, o: 1, d: 20 },
    ] },
    { n: 'b', b: 30, o: 3, d: 30, a: 14 },
    { n: '(other)', b: 10, o: 1, f: 4 },
  ],
}
const colors = (data: Awaited<ReturnType<typeof mapCard>>) => data.tiles.map(t => ({
  name: t.name, color: t.color, ...(t.kids ? { kids: t.kids.map(k => ({ name: k.name, color: k.color })) } : {}),
}))
const card = (c?: string) => mapCard({}, { name: 'Test storage', scheme: 'gs://' }, 'root', c ? { c } : {}, 'anon')

beforeEach(() => {
  mocks.tree.mockResolvedValue({ tree })
  mocks.meta.mockResolvedValue({ bytes: new TextEncoder().encode(JSON.stringify({
    users: [{ u: 'ann', b: 50 }], access: { from: 10, to: 30 },
  })) })
})

describe('map cards honor the dashboard color axis', () => {
  it('labels only maps with an explicitly public base read scope', async () => {
    expect([
      (await card('t')).publicLabels,
      (await mapCard({ PUBLIC_READ: '1' }, { name: 'Public demo', scheme: 'r2://' }, 'root', { c: 't' }, 'anon')).publicLabels,
      (await mapCard({}, { name: 'Private demo', scheme: 'r2://' }, 'root', { publicLabels: '1' }, 'anon')).publicLabels,
    ]).toEqual([undefined, true, undefined])
  })
  it.each(['t', 'tree'])('c=%s uses directory ranks and child hue fans, not owner colors', async c => {
    expect(colors(await card(c))).toEqual([
      { name: 'a', color: 'hsl(212 78% 52%)', kids: [{ name: 'a1', color: 'hsl(201 78% 45%)' }, { name: 'a2', color: 'hsl(223 78% 59%)' }] },
      { name: 'b', color: 'hsl(128 60% 38%)' },
      { name: '(other)', color: '#2b2e35' },
    ])
  })

  it.each(['w', 'written', 'date', 'age'])('c=%s uses the drawn leaves’ listing dates', async c => {
    expect(colors(await card(c))).toEqual([
      { name: 'a', color: '#232322', kids: [{ name: 'a1', color: 'rgb(68,1,84)' }, { name: 'a2', color: 'rgb(33,145,140)' }] },
      { name: 'b', color: 'rgb(253,231,37)' },
      { name: '(other)', color: '#2b2e35' },
    ])
  })

  it.each(['r', 'read'])('c=%s uses the logging window, distinguishing missing observations', async c => {
    expect(colors(await card(c))).toEqual([
      { name: 'a', color: '#232322', kids: [{ name: 'a1', color: 'rgb(68,1,84)' }, { name: 'a2', color: '#6e332b' }] },
      { name: 'b', color: 'rgb(61,66,128)' },
      { name: '(other)', color: '#2b2e35' },
    ])
  })

  it.each([undefined, 'u', 'user', 'invalid'])('c=%s retains owner coloring on attributed scans', async c => {
    expect(colors(await card(c))).toEqual([
      { name: 'a', color: '#4269d0', kids: [{ name: 'a1', color: '#4269d0' }, { name: 'a2', color: '#4a4943' }] },
      { name: 'b', color: '#4a4943' },
      { name: '(other)', color: '#2b2e35' },
    ])
  })

  it('unattributed scans default to tree instead of an entirely unowned gray card', async () => {
    mocks.meta.mockResolvedValue({ bytes: new TextEncoder().encode('{}') })
    expect(colors(await card())).toEqual(colors(await card('t')))
    expect((await card()).tiles[0].color).toBe('hsl(212 78% 52%)')
  })

  it('read mode without access-log coverage falls back to the available owner axis', async () => {
    mocks.meta.mockResolvedValue({ bytes: new TextEncoder().encode('{"users":[{"u":"ann","b":50}]}') })
    expect(await card('r')).toEqual(await card('u'))
  })

  it('configured written default applies unless the URL explicitly selects another mode', async () => {
    const site = { name: 'Test storage', scheme: 'r2://', defaultColor: 'date' as const }
    const defaultCard = await mapCard({}, site, 'root', {}, 'anon')
    expect(colors(defaultCard)).toEqual(colors(await card('w')))
    expect(defaultCard.subtitle).toBe('scan 10/13/26 10:00am UTC · color by modified')
    expect(colors(await mapCard({}, site, 'root', { c: 't' }, 'anon'))).toEqual(colors(await card('t')))
  })

  it('legends describe the selected axis, never owners on a written/read/tree card', async () => {
    expect((await card('t')).legend).toEqual([
      { label: 'a', b: 60, color: 'hsl(212 78% 52%)' },
      { label: 'b', b: 30, color: 'hsl(128 60% 38%)' },
    ])
    expect([(await card('w')).legend, (await card('r')).legend]).toEqual([[], []])
    expect((await card('u')).legend).toEqual([
      { label: 'ann', b: 50, color: '#4269d0' }, { label: 'unowned', b: 50, color: '#4a4943' },
    ])
  })
})
