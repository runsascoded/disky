// `/staged`'s card counts what the Slack parent counts (`countedItems`): the
// queued prefixes less those empty at the scan (and those a real run deleted).
import { describe, expect, it, vi } from 'vitest'
import type { PrefixStat } from '../prefixes.js'
import { sqliteD1 } from '../testD1.js'

const SCAN = '2026-10-08'
const TiB = 2 ** 40
// Every prefix holds 1 TiB / 10 objects owned by `ann-bee`, except those under `empty/` (nothing) and under `cw/`
// (unowned: a store without attribution; `big/` there holds 3 TiB).
vi.mock('../index.js', async orig => ({
  ...(await orig<typeof import('../index.js')>()),
  storeReady: () => true,
  pathScans: async () => ({ results: [{ date: SCAN }] }),
}))
vi.mock('../prefixes.js', async orig => ({
  ...(await orig<typeof import('../prefixes.js')>()),
  prefixesAt: async (_env: unknown, _date: string, prefixes: string[]) => {
    const stats: Record<string, PrefixStat> = {}
    for (const p of prefixes) {
      if (p.includes('/empty/')) continue
      stats[p] = p.includes('/cw/') ? { b: (p.includes('/big/') ? 3 : 1) * TiB, o: 10 } : { b: TiB, o: 10, us: [['ann-bee', TiB]] }
    }
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
const { cardSvg } = await import('./card.js')
const { HI_CONTRAST } = await import('../../../src/colors.js')
const { UNOWNED_COLOR } = await import('./data.js')
const textNodes = (svg: string) => [...svg.matchAll(/<text\b[^>]*>(.*?)<\/text>/g)].map(m => m[1])

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
      [['b', 2 * TiB, ['x/', 'y/']]],
    ])
  })

  it('unowned items (no attribution): each its own colour and label inside its bucket, and no owner legend', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['s3://b/cw/big/', 's3://b/cw/small/']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'cw', scheme: 's3' }, 'Staged', {}, 'full')
    expect([card.tiles.map(t => [t.name, t.color, t.kids?.map(k => [k.name, k.color])]), card.legend]).toEqual([
      [['b', UNOWNED_COLOR, [['cw/big/', HI_CONTRAST[0]], ['cw/small/', HI_CONTRAST[1]]]]],
      [],
    ])
    expect(textNodes(cardSvg(card))).toEqual(['cw', 'Staged', `plan #1 · scan ${SCAN}`, 'cw/big/ 3.00 TiB', 'cw/small/ 1.00 TiB', 'b 4.00 TiB', '2 prefixes · 4.00 TiB · 20 objects'])
  })
})
