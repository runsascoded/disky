// `/api/db`: the registry's tables are offered only where this deployment's D1 has them — a schema without
// `user_emails` / `actions` / `claims` (the test D1's) has them neither listed nor read (404, not a D1 error).
import { describe, expect, it } from 'vitest'
import { onRequest } from './[[path]]'
import { gateFor } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'

async function setup() {
  const { db, raw } = await sqliteD1()
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'base' }
  const token = (await gateFor(env as never)!.mint({
    email: 'ops@example.test', scopes: ['*'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null,
  })).token
  const call = async (path: string) => {
    const res = await onRequest({ request: new Request(`https://site.example.test/api/db${path}`, { headers: { authorization: `Bearer ${token}` } }), env } as never)
    return [res.status, await res.json()] as const
  }
  return { raw, call }
}

describe('/api/db', () => {
  it('lists only the registry tables the D1 has', async () => {
    const { call } = await setup()
    const [status, body] = await call('')
    expect(status).toBe(200)
    expect((body as { tables: { name: string }[] }).tables.map(t => t.name)).toEqual(['allowed_emails', 'deletion_runs', 'deletion_bands', 'admin_emails', 'admin_edits'])
  })

  it('a registry table the D1 lacks is a 404 naming it; a present one reads', async () => {
    const { raw, call } = await setup()
    expect(await call('/user_emails')).toEqual([404, { error: 'no user_emails table on this deployment' }])
    expect(await call('/nope')).toEqual([404, { error: 'unknown table' }])
    raw.prepare("INSERT INTO allowed_emails (email, note, read_only, who, ts) VALUES ('a@example.test', null, 0, 'ops', 1)").run()
    const [status, body] = await call('/allowed_emails')
    expect([status, (body as { rows: unknown[] }).rows]).toEqual([200, [{ email: 'a@example.test', note: null, read_only: 0, who: 'ops', ts: 1 }]])
  })
})
