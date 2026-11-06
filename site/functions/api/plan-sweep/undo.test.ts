// `POST /api/plan-sweep/undo`: a run whose bucket retained nothing, or one on a bucket now off, submits nothing.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { fakeR2 } from '../../_lib/testR2'
import { RUN_DIR, sqliteD1 } from '../../_lib/testD1'
import { onRequestPost } from './undo'

const RUN = 'cw-sweep-real-20261106-000000z'
const NOW = Math.floor(Date.now() / 1000)
const READING = (off: boolean) => ({
  'snapshots/soft-delete/2026-11-06.json': JSON.stringify({ version: 1, date: '2026-11-06', as_of: '2026-11-06T00:00:00Z', buckets: { b: { retention_seconds: off ? 0 : 604800 } } }),
})

let env: Record<string, unknown>
let token: string
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']

beforeEach(async () => {
  const d = await sqliteD1()
  raw = d.raw
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'actor', 1)")
  env = {
    DB: d.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', EXECUTOR: 'plan-sweep', GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'p', BATCH_REGION: 'r', DATA_BUCKET: 'data',
    SWEEP_IMAGE: 'image', SWEEP_S3_ENDPOINT: 'https://s3.example.test', STORE_SCHEME: 's3://', STORE_BUCKETS: 'b', SERVED_R2: '1', INDEX_R2: fakeR2(READING(false)),
  }
  token = (await gateFor(env as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
})
afterEach(() => vi.unstubAllGlobals())

function run(days: number | null, min: number | null, deadline: number | null): void {
  raw.prepare(`
    INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_digest, undo_deadline, soft_delete_days, min_soft_delete_days)
    VALUES (?, 1, 'm', 'scan', 0, 0, 'actor', 'real', 100, 200, 'logs', 'd', ?, ?, ?)`).run(RUN, deadline, days, min)
  raw.prepare("INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES (?, 's3://b/a/', 1, 1, 0, 0, 0, 0)").run(RUN)
}

async function post(): Promise<[number, unknown, number]> {
  const fetched = vi.fn(async () => new Response('{}', { status: 500 }))
  vi.stubGlobal('fetch', fetched)
  const r = await onRequestPost({
    request: new Request('https://site.example.test/api/plan-sweep/undo', {
      method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ run_id: RUN }),
    }),
    env: env as never,
  } as never)
  return [r.status, await r.json(), fetched.mock.calls.length]
}

describe('refused, nothing submitted', () => {
  it('a bucket that retained nothing', async () => {
    run(0, 0, null)
    expect(await post()).toEqual([409, { error: 'No undo: a bucket in this run had soft delete off, so what it deleted there is gone for good.' }, 0])
  })
  it('nothing required, and no retention recorded', async () => {
    run(null, 0, null)
    expect(await post()).toEqual([409, { error: 'No undo promised: this run required no soft delete and recorded no retention, so treat its deletes as permanent.' }, 0])
  })
  it('a bucket the newest reading shows off, inside its window', async () => {
    env.INDEX_R2 = fakeR2(READING(true))
    run(7, 7, NOW + 3600)
    expect(await post()).toEqual([409, { error: 'undo unavailable: soft delete is off on b (as of 2026-11-06 00:00Z)' }, 0])
  })
})
