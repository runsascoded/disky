// `scanDeletions`: which recorded bands a scan's sizes are out of date by.
import { describe, expect, it } from 'vitest'
import { scanDeletions } from './plans.js'
import { RUN_DIR, sqliteD1 } from './testD1.js'

const SCAN = '2026-11-04T0703', NEXT = '2026-11-05T0702'

async function bandsDb() {
  const { db, raw } = await sqliteD1()
  raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)")
  const run = raw.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id) VALUES (?, 'p', ?, 0, 0, 'ann', ?, 100, ?, 'd', 1)`)
  const band = raw.prepare('INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, undone_objects) VALUES (?, ?, 50, 5, 1, ?)')
  run.run('done', SCAN, 'real', 200); band.run('done', 'gs://b/done/', 0)
  run.run('undone', SCAN, 'real', 200); band.run('undone', 'gs://b/undone/', 1)
  run.run('dry', SCAN, 'dry', 200); band.run('dry', 'gs://b/dry/', 0)
  run.run('running', SCAN, 'real', null); band.run('running', 'gs://b/running/', 0)
  run.run('next', NEXT, 'real', 200); band.run('next', 'gs://b/next/', 0)
  return db
}

describe('scanDeletions', () => {
  it("a finished real run's bands on exactly that scan, none undone", async () => {
    expect(await scanDeletions(await bandsDb(), SCAN)).toEqual([{ prefix: 'gs://b/done/', bytes: 50, objects: 5, gone: 1 }])
  })
  it('a run on an earlier scan says nothing about a later one: what is there now may be written back', async () => {
    expect(await scanDeletions(await bandsDb(), '2026-11-06T0701')).toEqual([])
  })
})
