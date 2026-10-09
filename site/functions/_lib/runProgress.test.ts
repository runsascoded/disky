import { describe, expect, it } from 'vitest'
import { onRequestGet } from '../api/plans/progress'
import { sqliteD1 } from './testD1'

describe('D1 run-progress history', () => {
  it('returns chronological samples and rejects absent/unknown run IDs', async () => {
    const { db } = await sqliteD1('cw')
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    await db.prepare(`INSERT INTO deletion_runs (run_id, manifest, scan, head, exec_head, actor, mode, started_ts, finished_ts,
      deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, log_dir, plan_id)
      VALUES ('r', 'plan', 'scan', 0, 0, 'actor', 'real', 1, NULL, 0, 0, 0, 0, 0, 'dir', 1)`).run()
    const stmt = db.prepare('INSERT OR IGNORE INTO deletion_progress VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    await stmt.bind('r', 'b', 130, 30, 300, 0, 0, 0, 0, 1).run()
    await stmt.bind('r', 'b', 100, 0, 0, 0, 0, 0, 0, 0).run()
    await stmt.bind('r', 'b', 100, 0, 0, 0, 0, 0, 0, 0).run()
    const get = async (query: string) => {
      const r = await onRequestGet({ request: new Request(`http://localhost/api/plans/progress${query}`), env: { DB: db } })
      return [r.status, await r.json()]
    }
    expect(await get('?id=r')).toEqual([200, { samples: [
      { bucket: 'b', ts: 100, deletes: 0, bytes: 0, gone: 0, overwritten: 0, failed: 0, done: 0, bytes_exact: 0 },
      { bucket: 'b', ts: 130, deletes: 30, bytes: 300, gone: 0, overwritten: 0, failed: 0, done: 0, bytes_exact: 1 },
    ] }])
    expect(await get('')).toEqual([400, { error: 'id required' }])
    expect(await get('?id=unknown')).toEqual([404, { error: 'no such run' }])
  })
  it('gates unauthenticated remote readers', async () => {
    const r = await onRequestGet({ request: new Request('https://gcs.example/api/plans/progress?id=r'), env: {} })
    expect(r.status).toBe(401)
  })
})
