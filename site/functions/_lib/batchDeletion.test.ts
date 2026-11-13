import { describe, expect, it } from 'vitest'
import { emptiedBatches, planDetail, type PlanEdit, type StageBatchRow } from './plans'
import { RUN_DIR, sqliteD1 } from './testD1'
import { addBatchDeletion } from '../../src/batchDeletion'

const batch = (id: number): StageBatchRow => ({ id, plan_id: 1, created_by: 'actor', created_ts: id, note: null })
const stage = (id: number, prefixes: string[]): PlanEdit => ({ action: 'insert', old_json: null, new_json: JSON.stringify({ batch_id: id, staged: prefixes }) })
const complete = (run: string, prefixes: string[]): PlanEdit => ({ action: 'delete', old_json: JSON.stringify({ deleted_by: run, prefixes }), new_json: null })
const a = 'gs://b/a/', b = 'gs://b/b/'

describe('completed batch deletion totals', () => {
  it('binds each restaged path to its audited completing run and keeps missing bands unknown', () => {
    const batches = [batch(1), batch(2)]
    const edits = [stage(1, [a, b]), complete('first', [a, b]), stage(2, [a]), complete('second', [a])]
    const result = emptiedBatches(batches, [], edits, [
      { run_id: 'first', prefix: a, bytes: 100, objects: 3 },
      { run_id: 'second', prefix: a, bytes: 200, objects: 4 },
      { run_id: 'other', prefix: b, bytes: 999, objects: 99 },
    ])
    expect(result.map(e => ({ id: e.id, deleted: e.deleted, deletion: e.deletion }))).toEqual([
      { id: 1, deleted: 2, deletion: { bytes: 100, objects: 3, prefixes: 1 } },
      { id: 2, deleted: 1, deletion: { bytes: 200, objects: 4, prefixes: 1 } },
    ])
  })

  it('preserves zero-byte object counts through batch merges and staging-session coalescing', () => {
    const edits = [stage(1, [a]), stage(2, [b]), complete('run', [a, b]), {
      action: 'update', old_json: JSON.stringify({ batches: [2] }), new_json: JSON.stringify({ merged_into: 1 }),
    }]
    const bands = [{ run_id: 'run', prefix: a, bytes: 0, objects: 5 }, { run_id: 'run', prefix: b, bytes: 8, objects: 2 }]
    const merged = emptiedBatches([batch(1)], [], edits, bands)
    expect(merged.map(e => ({ deleted: e.deleted, deletion: e.deletion }))).toEqual([
      { deleted: 2, deletion: { bytes: 8, objects: 7, prefixes: 2 } },
    ])
    const separate = emptiedBatches([batch(1), batch(2)], [], edits.slice(0, 3), bands)
    expect(separate.map(e => e.deletion)).toEqual([
      { bytes: 0, objects: 5, prefixes: 1 }, { bytes: 8, objects: 2, prefixes: 1 },
    ])
    expect(addBatchDeletion(separate[0].deletion, separate[1].deletion)).toEqual(merged[0].deletion)
    expect(addBatchDeletion(undefined, undefined)).toBeUndefined()
  })

  it('loads only finished real bands from this plan', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'staged','actor',1),(2,'other','actor',1)").run()
    await db.prepare("INSERT INTO stage_batches (id,plan_id,created_by,created_ts) VALUES (1,1,'actor',1)").run()
    for (const [run, mode, plan, end] of [['real', 'real', 1, 10], ['dry', 'dry', 1, 10], ['live', 'real', 1, null], ['other', 'real', 2, 10]] as const) {
      await db.prepare(`INSERT INTO deletion_runs (run_id,${RUN_DIR},scan,head,exec_head,actor,mode,started_ts,finished_ts,log_dir,plan_id) VALUES (?,'p','scan',0,0,'actor',?,2,?,'dir',?)`).bind(run, mode, end, plan).run()
      await db.prepare('INSERT INTO deletion_bands (run_id,prefix,bytes,objects) VALUES (?,?,50,5)').bind(run, `gs://b/${run}/`).run()
    }
    const paths = ['real', 'dry', 'live', 'other'].map(r => `gs://b/${r}/`)
    const edits = [stage(1, paths), ...paths.map((p, i) => complete(['real', 'dry', 'live', 'other'][i], [p]))]
    for (const e of edits) await db.prepare("INSERT INTO admin_edits (ts,who,tbl,pk,action,old_json,new_json) VALUES (2,'actor','plan_items','1',?,?,?)").bind(e.action, e.old_json, e.new_json).run()
    const d = (await planDetail(db, 1, true))!
    expect(d.emptied.map(e => ({ deleted: e.deleted, deletion: e.deletion }))).toEqual([
      { deleted: 4, deletion: { bytes: 50, objects: 5, prefixes: 1 } },
    ])
  })
})
