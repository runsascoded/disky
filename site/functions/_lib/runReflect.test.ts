import { afterEach, describe, expect, it, vi } from 'vitest'
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

describe('undo_state', () => {
  const RUN = 'cw-sweep-real-test'
  const CREATED = '2026-11-05T00:00:00Z'
  const AFTER = Date.parse(CREATED) / 1000 + 60
  const summary = (restored: number, restore_failed = 0, unrecoverable = 0, finished_ts = AFTER) => ({ dry_run: false, restored, restore_failed, unrecoverable, finished_ts })
  const undoJob = (id: string, state: string, createTime = CREATED): BatchJob => ({
    name: `projects/p/locations/r/jobs/${id}`, uid: id, createTime, status: { state },
    taskGroups: [{ taskSpec: { environment: { variables: { OP: 'undo', TARGET_RUN: RUN } } } }],
  })
  async function settled(jobs: BatchJob[], body: object | null, before = 'none'): Promise<string> {
    const { db, raw } = await sqliteD1()
    raw.exec(`INSERT INTO plans (id,name,created_by,created_ts) VALUES (1,'review','actor',1);
      INSERT INTO deletion_runs (run_id,plan_id,${RUN_DIR},scan,head,exec_head,actor,mode,started_ts,finished_ts,log_dir,plan_digest,undo_state)
      VALUES ('${RUN}',1,'manifest','scan',0,0,'actor','real',100,200,'logs','review','${before}')`)
    vi.stubGlobal('fetch', vi.fn(async () => body ? new Response(JSON.stringify(body)) : new Response('', { status: 404 })))
    await reflectRuns({ dataBucket: 'data' } as BatchConfig, db, 'token', jobs, 'b')
    return (raw.prepare('SELECT undo_state FROM deletion_runs').all() as { undo_state: string }[])[0].undo_state
  }

  it.each([
    ['succeeded, all back', 'SUCCEEDED', summary(3), 'none', 'full'],
    ['a retry finishes it', 'SUCCEEDED', summary(1), 'partial', 'full'],
    ['failed, some back', 'FAILED', summary(2, 1), 'none', 'partial'],
    ['killed partway', 'CANCELLED', summary(2), 'none', 'partial'],
    ['failed, nothing back', 'FAILED', summary(0, 3), 'none', 'none'],
    ['the bucket kept nothing', 'FAILED', summary(0, 0, 3), 'none', 'none'],
    ['a retry restores nothing', 'FAILED', summary(0, 1), 'partial', 'partial'],
    ['died before any summary', 'FAILED', null, 'none', 'none'],
    ['only an earlier undo\'s summary', 'FAILED', summary(5, 0, 0, AFTER - 3600), 'none', 'none'],
    ['still running', 'RUNNING', summary(2), 'none', 'none'],
  ] as const)('%s', async (_, state, body, before, after) => {
    expect(await settled([undoJob('cw-undo-1', state)], body, before)).toBe(after)
  })

  it('only the newest undo counts: an older success doesn\'t speak for a running retry', async () => {
    const older = undoJob('cw-undo-1', 'SUCCEEDED', '2026-11-04T00:00:00Z')
    expect(await settled([undoJob('cw-undo-2', 'RUNNING'), older], summary(2), 'partial')).toBe('partial')
  })
})
