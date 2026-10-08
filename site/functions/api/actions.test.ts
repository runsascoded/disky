// `/api/actions` exists only where the deployment keeps an ownership ledger
// (`hasLedger`): cw's D1 has no `actions` / `owner_prefixes`, so both methods
// are a 404 there rather than a 500 from a missing table.
import { describe, expect, it } from 'vitest'
import { onRequest } from './actions'
import { sqliteD1 } from '../_lib/testD1'

const get = new Request('http://localhost/api/actions')
const post = new Request('http://localhost/api/actions', {
  method: 'POST',
  body: JSON.stringify({ pattern: 's3://b/tmp/', owner: '@me' }),
})
const answer = async (r: Response) => [r.status, await r.json()]

describe('/api/actions without a ledger', () => {
  it('is a 404 for both methods on the cw lineage', async () => {
    const { db } = await sqliteD1('cw')
    const env = { DB: db, STORE_SCHEME: 's3://', STORE_BUCKETS: 'b' }
    expect([
      await onRequest({ request: get, env } as never).then(answer),
      await onRequest({ request: post, env } as never).then(answer),
    ]).toEqual([
      [404, { error: 'no ownership ledger on this deployment' }],
      [404, { error: 'no ownership ledger on this deployment' }],
    ])
  })

  it('serves the live owners where the ledger tables exist', async () => {
    const { db } = await sqliteD1('cw')
    await db.prepare('CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT, memo TEXT)').run()
    await db.prepare('CREATE TABLE owner_prefixes (prefix TEXT, owner TEXT, ts REAL, action_id INTEGER, tombstoned REAL)').run()
    await db.prepare("INSERT INTO actions (id, actor, memo) VALUES (1, 'ann', 'mine')").run()
    await db.prepare("INSERT INTO owner_prefixes (prefix, owner, ts, action_id) VALUES ('s3://b/tmp/', 'ann', 5, 1)").run()
    expect(await onRequest({ request: get, env: { DB: db } } as never).then(answer)).toEqual([200, {
      owners: [{ prefix: 's3://b/tmp/', owner: 'ann', ts: 5, who: 'ann', memo: 'mine', action_id: 1 }],
    }])
  })
})
