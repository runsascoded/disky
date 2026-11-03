import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mapCard } from './data'
import type { TreeNode } from '../../../src/types'

// The live ownership ledger over a map card's tree (`applyLedger` in `mapCard`): the root card
// (`/`, path '', drawn to depth 2) colors an assignment as the bucket's drill card does — one at a
// drawn path, and one strictly inside a tile the root's depth cap leaves childless.
const mocks = vi.hoisted(() => ({ tree: vi.fn(), meta: vi.fn(), ledger: vi.fn(), assignments: vi.fn() }))
vi.mock('../index', () => ({
  pathScans: async () => ({ results: [{ date: '2026-10-13T1000' }] }),
  servedStore: () => ({ get: mocks.meta }),
}))
vi.mock('../scanTimes', () => ({ scanTimes: async () => ({}) }))
vi.mock('../identity', () => ({ loadRegistry: async () => ({}), canonId: (s: string) => s }))
vi.mock('../staticFilter', () => ({ hexQuery: async (_env: unknown, query: unknown) => ({ query }) }))
vi.mock('../ledger', () => ({ loadLedger: mocks.ledger }))
vi.mock('../ownerTotals', () => ({ ownerAssignments: mocks.assignments }))
vi.mock('../view', () => ({ buildView: mocks.tree, ATTEN_DEFAULT: 0.5, MIN_AREA_DEFAULT: 20, NotFound: class extends Error {} }))

// `docs` at the depth cap (the root view's depth 2) comes back childless; the drill view draws `docs/sub`.
const bucketA = (drill: boolean): TreeNode => ({ n: 'bkt-a', k: 'dir', b: 100, o: 10, us: [['ann', 60]], c: [
  { n: 'ckpt', k: 'dir', b: 60, o: 6, us: [['ann', 60]] },
  { n: 'docs', k: 'dir', b: 40, o: 4, ...(drill ? { c: [
    { n: 'sub', k: 'dir', b: 30, o: 3 },
    { n: '(other)', b: 10, o: 1, f: 2 },
  ] } : {}) },
] })
const rootTree = (): TreeNode => ({ n: 'all buckets', k: 'dir', b: 150, o: 15, us: [['ann', 60]], c: [
  bucketA(false),
  { n: 'bkt-b', k: 'dir', b: 50, o: 5 },
] })

const site = { name: 'Test storage', scheme: 'gs://' }
const env = { DB: {} } as never
const tiles = (data: Awaited<ReturnType<typeof mapCard>>) => data.tiles.map(t => ({
  name: t.name, color: t.color, ...(t.kids ? { kids: t.kids.map(k => ({ name: k.name, color: k.color })) } : {}),
}))

beforeEach(() => {
  mocks.meta.mockResolvedValue({ bytes: new TextEncoder().encode(JSON.stringify({ users: [{ u: 'ann', b: 60 }, { u: 'bo', b: 0 }] })) })
  mocks.tree.mockImplementation(async (_env: unknown, o: { path: string }) => ({ tree: o.path === '' ? rootTree() : bucketA(true) }))
})
const ledger = (prefix: string, bytes: number) => {
  mocks.ledger.mockResolvedValue({ ownerRows: [{ prefix, kind: 'prefix', owner: 'bo', ts: 1, who: 'admin', action_id: 1 }], head: 1 })
  mocks.assignments.mockResolvedValue([{ prefix, owner: 'bo', who: 'admin', ts: 1, action_id: 1, bytes, objects: 3, us: {}, uo: {} }])
}

describe('map cards apply the ownership ledger at every depth', () => {
  it('a depth-2 assignment is drawn owned on the root card, as on the bucket card', async () => {
    ledger('gs://bkt-a/docs/', 40)
    const root = await mapCard(env, site, 'root', {}, 'full', { deepen: false })
    const drill = await mapCard(env, site, 'bkt-a', { path: 'bkt-a' }, 'full', { deepen: false })
    expect(tiles(root)).toEqual([
      { name: 'bkt-a', color: '#4269d0', kids: [{ name: 'ckpt', color: '#4269d0' }, { name: 'docs', color: '#efb118' }] },
      { name: 'bkt-b', color: '#4a4943' },
    ])
    expect(tiles(drill)).toEqual([
      { name: 'ckpt', color: '#4269d0' },
      { name: 'docs', color: '#efb118', kids: [{ name: 'sub', color: '#efb118' }, { name: '(other)', color: '#2b2e35' }] },
    ])
    expect(root.legend).toEqual([
      { label: 'ann', b: 60, color: '#4269d0' },
      { label: 'unowned', b: 50, color: '#4a4943' },
      { label: 'bo', b: 40, color: '#efb118' },
    ])
  })

  it('an assignment inside a depth-capped tile moves its bytes on the root card, as the drill card draws them', async () => {
    ledger('gs://bkt-a/docs/sub/', 30)
    const root = await mapCard(env, site, 'root', {}, 'full', { deepen: false })
    const drill = await mapCard(env, site, 'bkt-a', { path: 'bkt-a' }, 'full', { deepen: false })
    expect(tiles(drill)).toEqual([
      { name: 'ckpt', color: '#4269d0' },
      { name: 'docs', color: '#efb118', kids: [{ name: 'sub', color: '#efb118' }, { name: '(other)', color: '#2b2e35' }] },
    ])
    expect(drill.legend).toEqual([
      { label: 'ann', b: 60, color: '#4269d0' },
      { label: 'bo', b: 30, color: '#efb118' },
      { label: 'unowned', b: 10, color: '#4a4943' },
    ])
    expect(tiles(root)).toEqual([
      { name: 'bkt-a', color: '#4269d0', kids: [{ name: 'ckpt', color: '#4269d0' }, { name: 'docs', color: '#efb118' }] },
      { name: 'bkt-b', color: '#4a4943' },
    ])
    expect(root.legend).toEqual([
      { label: 'ann', b: 60, color: '#4269d0' },
      { label: 'unowned', b: 60, color: '#4a4943' },
      { label: 'bo', b: 30, color: '#efb118' },
    ])
  })
})
