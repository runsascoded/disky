import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { actionLog } from './actionLog'
import { sqliteD1, withLedger } from './testD1'

describe('actionLog: every owner action, newest first, with what became of it', () => {
  it('live, superseded, overridden by an ancestor, retracted — and `_` is not a wildcard', async () => {
    const { db, raw } = await sqliteD1()
    withLedger(raw)
    const act = (id: number, prefix: string, owner: string | null, memo: string | null = null) => raw.exec(
      `INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner, memo) VALUES (${id}, 'u${id}@x', ${100 + id}, 's', '${prefix}', 1, ${owner ? `'${owner}'` : 'NULL'}, ${memo ? `'${memo}'` : 'NULL'});` +
      `INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (${id}, '${prefix}', ${owner ? `'${owner}'` : 'NULL'}, ${100 + id});`,
    )
    act(1, 'gs://b/x/deep/', 'mia')
    act(2, 'gs://b/x/', 'kim')
    act(3, 'gs://b/x/', 'lee', 'reassigned')
    act(4, 'gs://b/a_c/', 'mia')
    act(5, 'gs://b/abc/', 'kim')   // `a_c` LIKE-matches `abc`; must not override it
    act(6, 'gs://b/', 'ryan')
    raw.exec(`UPDATE owner_prefixes SET tombstoned = 'retracted: bug' WHERE action_id = 6`)
    const { total, rows } = await actionLog({ DB: db } as Env, 10, 0)
    expect([total, rows.map(r => [r.id, r.prefix, r.owner, r.status, r.retracted, r.memo])]).toEqual([6, [
      [6, 'gs://b/', 'ryan', 'retracted', 'retracted: bug', null],
      [5, 'gs://b/abc/', 'kim', 'live', null, null],
      [4, 'gs://b/a_c/', 'mia', 'live', null, null],
      [3, 'gs://b/x/', 'lee', 'live', null, 'reassigned'],
      [2, 'gs://b/x/', 'kim', 'superseded', null, null],
      [1, 'gs://b/x/deep/', 'mia', 'overridden', null, null],
    ]])
    expect((await actionLog({ DB: db } as Env, 2, 2)).rows.map(r => r.id)).toEqual([4, 3])
  })
})
