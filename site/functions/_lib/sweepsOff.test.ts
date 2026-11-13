// `SWEEP_DISPATCH = "0"` (`sweepsOff`) on the routes that act on a sweep's jobs or data, or on the real digest,
// outside `dispatchPlan` / `undoSweepRun` (`executor.test.ts` covers those): each refuses after its auth gate, before any
// GCP call; unset, each goes on to the call.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from './auth'
import { SWEEPS_OFF } from './dispatch'
import { RUN_DIR, sqliteD1 } from './testD1'
import { onRequestPost as purge } from '../api/plan-sweep/purge'
import { onRequestPost as planSweepStop } from '../api/plan-sweep/stop'
import { onRequestPost as sweepStop } from '../api/sweep/stop'
import { onRequestPost as digestReapply } from '../api/digest-reapply'

vi.mock('./gcp.js', async original => ({ ...await original<typeof import('./gcp.js')>(), gcpToken: async () => 'token' }))

const CW_RUN = 'cw-sweep-real-20261106-000000z'
const GCS_RUN = 'gcs-sweep-real-20261106-000000z'
const DIGEST_REAPPLY = JSON.stringify({ template: 't', config: 'job/digest.yml', root: 'snapshots/s', env_script: 'job/env.sh', r2_bucket: 'r2b', secrets: { SITE_TOKEN: 'site-token' } })

let env: Record<string, unknown>
let token: string

beforeEach(async () => {
  const { db, raw } = await sqliteD1()
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'actor', 1)")
  // A real run past its undo hold, pending purge: everything `/api/plan-sweep/purge` checks before it submits.
  raw.prepare(`
    INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, undo_deadline, purge_state)
    VALUES (?, 1, 'm', 'scan', 0, 0, 'actor', 'real', 100, 200, 'logs', 300, 'pending')`).run(CW_RUN)
  env = {
    DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'p', BATCH_REGION: 'r', DATA_BUCKET: 'data',
    SWEEP_IMAGE: 'image', SWEEP_S3_ENDPOINT: 'https://s3.example.test', STORE_SCHEME: 's3://', STORE_BUCKETS: 'b', DIGEST_REAPPLY,
  }
  token = (await gateFor(env as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
})
afterEach(() => vi.unstubAllGlobals())

type Route = (ctx: never) => Promise<Response>

/** The route's status and body, and the URLs it fetched (every job id's stamp normalized, run ids' too). */
async function post(route: Route, path: string, body: unknown): Promise<[number, unknown, string[]]> {
  const fetched = vi.fn(async (_url: string, _init?: unknown) => new Response('{}', { status: 500 }))
  vi.stubGlobal('fetch', fetched)
  const r = await route({
    request: new Request(`https://site.example.test${path}`, {
      method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(body),
    }),
    env: env as never,
  } as never)
  return [r.status, await r.json(), fetched.mock.calls.map(([url]) => String(url).replace(/\d{8}-\d{6}z/g, '<stamp>z'))]
}

const ROUTES: [string, Route, string, unknown][] = [
  ['purge', purge as Route, '/api/plan-sweep/purge', { run_id: CW_RUN }],
  ['plan-sweep stop', planSweepStop as Route, '/api/plan-sweep/stop', { job_id: CW_RUN }],
  ['sweep stop', sweepStop as Route, '/api/sweep/stop', { job_id: GCS_RUN }],
  ['digest re-apply', digestReapply as Route, '/api/digest-reapply', { month: '2026-11', channel: 'C01234567', target: 'thread', dry_run: true }],
]

describe('`SWEEP_DISPATCH = "0"`', () => {
  it.each(ROUTES)('%s: refused after the auth gate, nothing fetched', async (_name, route, path, body) => {
    env.SWEEP_DISPATCH = '0'
    expect(await post(route, path, body)).toEqual([403, { error: SWEEPS_OFF }, []])
  })
})

describe('`SWEEP_DISPATCH` unset: past the guard, to the GCP call', () => {
  it('purge submits its Batch job', async () => {
    expect(await post(purge as Route, '/api/plan-sweep/purge', { run_id: CW_RUN })).toEqual([
      500, { error: 'batch submit failed (500)', status: 500 },
      ['https://batch.googleapis.com/v1/projects/p/locations/r/jobs?job_id=cw-purge-<stamp>z'],
    ])
  })
  it('plan-sweep stop cancels the job', async () => {
    expect(await post(planSweepStop as Route, '/api/plan-sweep/stop', { job_id: CW_RUN })).toEqual([
      500, { error: 'cancel failed', status: 500, detail: '{}' },
      ['https://batch.googleapis.com/v1/projects/p/locations/r/jobs/cw-sweep-real-<stamp>z:cancel'],
    ])
  })
  it('sweep stop writes STOP', async () => {
    expect(await post(sweepStop as Route, '/api/sweep/stop', { job_id: GCS_RUN })).toEqual([
      500, { error: 'STOP write failed', status: 500, detail: '{}' },
      ['https://storage.googleapis.com/upload/storage/v1/b/data/o?uploadType=media&name=sweep%2Fruns%2Fgcs-sweep-real-<stamp>z%2FSTOP'],
    ])
  })
  it('digest re-apply submits its Batch job', async () => {
    expect(await post(digestReapply as Route, '/api/digest-reapply', { month: '2026-11', channel: 'C01234567', target: 'thread', dry_run: true })).toEqual([
      500, { error: 'batch submit failed (500)', detail: '{}' },
      ['https://batch.googleapis.com/v1/projects/p/locations/r/jobs?job_id=t-digest-<stamp>z'],
    ])
  })
})
