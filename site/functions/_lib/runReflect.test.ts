import { afterEach, expect, it, vi } from 'vitest'
import type { BatchConfig } from './batchConfig'
import { reflectRuns, type BatchJob } from './runReflect'
import { RUN_DIR, sqliteD1 } from './testD1'

afterEach(() => vi.unstubAllGlobals())

it.each([
  ['RUNNING', 0, [], null, 'review'],
  ['SUCCEEDED', 0, [{ run_id: 'cw-sweep-real-test', ok: true }], 200, 'review'],
  ['SUCCEEDED', 1, [{ run_id: 'cw-sweep-real-test', ok: false }], 200, ''],
  ['FAILED', 0, [{ run_id: 'cw-sweep-real-test', ok: false }], 200, ''],
] as const)('reflects terminal outcome honestly: %s, %i failed deletions', async (state, failures, expected, finished, digest) => {
  const { db, raw } = await sqliteD1()
  raw.exec(`INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1);
    INSERT INTO deletion_runs (run_id,plan_id,${RUN_DIR},scan,head,exec_head,actor,mode,started_ts,log_dir,plan_digest)
    VALUES ('cw-sweep-real-test',1,'manifest','scan',0,0,'actor','real',100,'logs','review')`)
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    mode: 'real', bucket: 'b', deleted_objects: 1, deleted_bytes: 10, skipped_gone: 0,
    skipped_overwritten: 0, drift_new: 9, delete_failed: failures, finished_ts: 200, bands: [],
  }))))
  const jobs: BatchJob[] = [{ name: 'projects/p/locations/r/jobs/cw-sweep-real-test', uid: 'uid', createTime: '2026-10-21T00:00:00Z', status: { state } }]
  expect(await reflectRuns({ dataBucket: 'data' } as BatchConfig, db, 'token', jobs, 'b')).toEqual(expected)
  expect(raw.prepare('SELECT finished_ts,plan_digest FROM deletion_runs').all()).toEqual([{ finished_ts: finished, plan_digest: digest }])
})
