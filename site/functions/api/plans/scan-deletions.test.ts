// `GET /api/plans/scan-deletions`: a viewer's read, like the plan it sizes.
import { describe, expect, it } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { RUN_DIR, sqliteD1 } from '../../_lib/testD1'
import { onRequestGet } from './scan-deletions'

const SCAN = '2026-11-04T0703'

async function get(query: string, scopes: string[] | null) {
  const { db, raw } = await sqliteD1()
  raw.exec(`INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1);
    INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, plan_digest, log_dir, plan_id)
    VALUES ('r', 'p', '${SCAN}', 0, 0, 'ann', 'real', 100, 200, 'D', 'd', 1);
    INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone) VALUES ('r', 'gs://b/x/', 50, 5, 1)`)
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  const token = scopes && (await gateFor(env as never)!.mint({ name: 'caller', scopes, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const r = await onRequestGet({
    request: new Request(`https://site.example.test/api/plans/scan-deletions${query}`, { headers: token ? { authorization: `Bearer ${token}` } : {} }),
    env: env as never,
  })
  return [r.status, await r.json()]
}

describe('GET /api/plans/scan-deletions', () => {
  it('refuses a caller with no credentials', async () => {
    expect(await get(`?scan=${SCAN}`, null)).toEqual([401, { error: 'unauthenticated' }])
  })
  it("refuses a grant that isn't this deployment's viewer", async () => {
    expect(await get(`?scan=${SCAN}`, ['gcs:read'])).toEqual([403, { error: 'forbidden' }])
  })
  it('answers any viewer, a read-only one included', async () => {
    expect([await get(`?scan=${SCAN}`, ['cw:read']), await get(`?scan=${SCAN}`, ['cw'])]).toEqual([
      [200, { bands: [{ prefix: 'gs://b/x/', bytes: 50, objects: 5, gone: 1 }] }],
      [200, { bands: [{ prefix: 'gs://b/x/', bytes: 50, objects: 5, gone: 1 }] }],
    ])
  })
  it('needs the scan', async () => {
    expect(await get('', ['cw:read'])).toEqual([400, { error: 'scan required' }])
  })
})
