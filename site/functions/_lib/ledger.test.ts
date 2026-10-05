import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { ledgerHead, loadLedger } from './ledger'
import { sqliteD1 } from './testD1'

// The ledger tables as gcs's lineage has them (`migrations/gcs/0001_init.sql`,
// keep columns dropped by 0028); the shared `cw` lineage has no ledger.
const LEDGER = `
  CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT, memo TEXT);
  CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, PRIMARY KEY (prefix, action_id));
`

describe('the ledger head', () => {
  it('moves on a retraction (a tombstoned row), not only on a new action', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(LEDGER)
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
