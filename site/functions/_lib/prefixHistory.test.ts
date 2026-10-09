import { expect, it } from 'vitest'
import { sqliteD1 } from './testD1'
import { onRequestGet } from '../api/plans/prefix-history'

it('returns only real outcomes after the current staging, preserving exact prefixes', async () => {
  const { db, raw } = await sqliteD1('cw')
  raw.exec(`INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'staged','actor',1);
    INSERT INTO plan_items (plan_id,prefix,added_by,added_ts) VALUES (1,'gs://b/a/','actor',100);`)
  const run = db.prepare(`INSERT INTO deletion_runs (run_id,manifest,scan,head,exec_head,actor,mode,started_ts,finished_ts,log_dir,plan_id) VALUES (?,'p','scan',0,0,'actor',?,?,200,'dir',1)`)
  const band = db.prepare('INSERT INTO deletion_bands (run_id,prefix,bytes,objects) VALUES (?,?,50,5)')
  for (const [id, mode, ts, prefix] of [['old','real',99,'gs://b/a/'], ['dry','dry',101,'gs://b/a/'], ['sibling','real',101,'gs://b/ab/'], ['real','real',101,'gs://b/a/']] as const) {
    await run.bind(id, mode, ts).run()
    await band.bind(id, prefix).run()
  }
  const get = async (query: string) => {
    const r = await onRequestGet({ request: new Request(`http://localhost/api/plans/prefix-history${query}`), env: { DB: db } })
    return [r.status, await r.json()]
  }
  expect(await get('?id=1')).toEqual([200, { bands: [{ run_id: 'real', prefix: 'gs://b/a/', bytes: 50, objects: 5, gone: 0, overwritten: 0, drift_new_objects: 0, undone_objects: 0, started_ts: 101, finished_ts: 200, undo_deadline: null, undo_state: 'none' }] }])
  expect(await get('?id=bad')).toEqual([400, { error: 'positive plan id required' }])
  expect(await get('')).toEqual([400, { error: 'positive plan id required' }])
})
it('gates unauthenticated readers before D1 access', async () => {
  const r = await onRequestGet({ request: new Request('https://gcs.example/api/plans/prefix-history?id=1'), env: {} })
  expect(r.status).toBe(401)
})
