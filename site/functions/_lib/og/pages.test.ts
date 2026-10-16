// `/staged`'s card counts what the Slack parent counts (`countedItems`): the
// queued prefixes less those empty at the scan (and those a real run deleted).
import { describe, expect, it, vi } from 'vitest'
import type { PrefixStat } from '../prefixes.js'
import { sqliteD1 } from '../testD1.js'

const SCAN = '2026-10-08'
const TiB = 2 ** 40
// Every prefix holds 1 TiB / 10 objects owned by `ann-bee`, except those under `empty/`.
vi.mock('../index.js', async orig => ({
  ...(await orig<typeof import('../index.js')>()),
  storeReady: () => true,
  pathScans: async () => ({ results: [{ date: SCAN }] }),
}))
vi.mock('../prefixes.js', async orig => ({
  ...(await orig<typeof import('../prefixes.js')>()),
  prefixesAt: async (_env: unknown, _date: string, prefixes: string[]) => {
    const stats: Record<string, PrefixStat> = {}
    for (const p of prefixes) if (!p.includes('/empty/')) stats[p] = { b: TiB, o: 10, us: [['ann-bee', TiB]] }
    return { stats, groups: 1 }
  },
}))
vi.mock('../identity.js', async orig => ({
  ...(await orig<typeof import('../identity.js')>()),
  loadRegistry: async () => ({ 'ann-bee': { name: 'Ann Bee' } }),
}))
vi.mock('../ledger.js', () => ({ loadLedger: async () => ({ ownerRows: [] }) }))
vi.mock('../ownerTotals.js', () => ({ ownerTotals: async () => ({ users: { 'ann-bee': { b: TiB } } }) }))

const { stagedCard } = await import('./pages.js')

describe('stagedCard', () => {
  it('counts the prefixes the Slack parent counts: those empty at the scan are left out', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['gs://b/x/', 'gs://b/y/', 'gs://b/empty/a/', 'gs://b/empty/b/']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'gcs', scheme: 'gs' }, 'Staged', {}, 'full')
    expect([card.subtitle, card.total, card.tiles.map(t => [t.name, t.b, t.kids?.map(k => k.name)])]).toEqual([
      `plan #1 · scan ${SCAN}`,
      '2 prefixes · 2.00 TiB · 20 objects',
      [['b', 2 * TiB, ['gs://b/x/', 'gs://b/y/']]],
    ])
  })
})
