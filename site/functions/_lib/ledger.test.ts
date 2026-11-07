import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { ledgerHead, loadLedger } from './ledger'
import { sqliteD1, withLedger } from './testD1'

describe('the ledger head', () => {
  it('moves on a retraction (a tombstoned row), not only on a new action', async () => {
    const { db, raw } = await sqliteD1()
    withLedger(raw)
    const env = { DB: db } as Env
    const assign = (id: number, prefix: string, owner: string) => raw.exec(
      `INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES (${id}, 'a', ${id}, 's', '${prefix}', 1, '${owner}');` +
      `INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (${id}, '${prefix}', '${owner}', ${id});`,
    )
    expect(await ledgerHead(env)).toBe(0)
    assign(1, 'gs://b/x/', 'u1')
    assign(2, 'gs://b/', 'u2')
    const h2 = await ledgerHead(env)
    raw.exec(`UPDATE owner_prefixes SET tombstoned = 'retracted' WHERE action_id = 2`)
    const h3 = await ledgerHead(env)
    assign(3, 'gs://b/y/', 'u3')
    const h4 = await ledgerHead(env)
    // Strictly increasing: each head keys the `owner_totals` it was folded at.
    expect([h2, h3, h4]).toEqual([2, 3, 4])
    const { ownerRows, head } = await loadLedger(env)
    expect([head, ownerRows.map(r => [r.prefix, r.owner])]).toEqual([4, [['gs://b/x/', 'u1'], ['gs://b/y/', 'u3']]])
  })
})
