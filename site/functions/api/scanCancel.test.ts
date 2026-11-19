// `POST /api/scan-runs/cancel` ("Cancel" on a running scan, `_lib/scanCancel.ts`): the head first, then every live job
// labelled with the scan and the deployment, found over all locations; the run marked failed; what refuses it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type Env, gateFor } from '../_lib/auth'
import { jobsFilter } from '../_lib/scanCancel'
import { type Sqlite, sqliteD1, withAdminEmails } from '../_lib/testD1'
import { onRequestPost as cancel } from './scan-runs/cancel'

vi.mock('../_lib/gcp', async orig => ({ ...(await orig<typeof import('../_lib/gcp')>()), gcpToken: async () => 'tok' }))

const SITE = 'https://gcs.example.test'
const NOW = 1_800_000_000
const ADMIN = 'lead@example.org'
const P = 'projects/demo-project/locations'
const HEAD = `${P}/us-central1/jobs/demo-snapshot-20261119-123925`
const LIST = `https://batch.googleapis.com/v1/${P}/-/jobs`

beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW * 1000) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

interface Call { method: string; url: string; body: unknown }

/** Batch's listing of the scan's jobs: the head (no scan label in reality, but a listing that includes it must not
 *  cancel it twice), two live tasks in another region, one finished. */
const JOBS = [
  { name: HEAD, status: { state: 'RUNNING' } },
  { name: `${P}/us-east1/jobs/nf-post`, status: { state: 'RUNNING' } },
  { name: `${P}/us-east1/jobs/iv-merge`, status: { state: 'QUEUED' } },
  { name: `${P}/us-east1/jobs/nf-list`, status: { state: 'SUCCEEDED' } },
]

async function world(answer: (url: string, method: string) => Response = (u, m) => (m === 'GET' ? Response.json({ jobs: JOBS }) : Response.json({})), extra: Record<string, unknown> = {}) {
  const { db, raw } = await sqliteD1()
  withAdminEmails(raw)
  raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(ADMIN)
  const env = {
    DB: db, SESSION_SECRET: 'secret', BASE_SCOPE: 'gcs', ADMIN_LIST: '1', GCP_SA_KEY: '{}', GCP_PROJECT: 'demo-project',
    DISKY_LABELS: 'app=disky,deployment=demo', ...extra,
  } as Env
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    const method = init.method ?? 'GET'
    calls.push({ method, url, body: init.body ? JSON.parse(String(init.body)) : null })
    return answer(url, method)
  })
  const cookie = (await gateFor(env)!.signIn(ADMIN, new Request(SITE)))!.cookie.split(';')[0]
  const post = async (body: unknown): Promise<[number, unknown]> => {
    const r = await cancel({ request: new Request(`${SITE}/api/scan-runs/cancel`, { method: 'POST', headers: { cookie, 'content-type': 'application/json' }, body: JSON.stringify(body) }), env })
    return [r.status, await r.json()]
  }
  return { raw, calls, post }
}

const run = (raw: Sqlite, id: string, { kind = 'scan', status = 'running', parent = null as string | null, job = 'demo-snapshot-20261119-123925' as string | null } = {}) =>
  raw.prepare(`INSERT INTO scan_runs (run_id, scan, kind, parent, status, started_ts, job_name, region, source, updated_ts)
      VALUES (?, '2026-11-19T1241', ?, ?, ?, ?, ?, 'us-central1', 'job', ?)`).run(id, kind, parent, status, NOW - 3600, job, NOW - 60)

const row = (raw: Sqlite, id: string) => raw.prepare('SELECT status, error, finished_ts, updated_ts FROM scan_runs WHERE run_id = ?').all(id)

const REASON = { reason: `cancelled by ${ADMIN} from /scans` }

describe('POST /api/scan-runs/cancel', () => {
  it('cancels the head, then every live job of the scan in any region, and marks the run failed', async () => {
    const w = await world()
    run(w.raw, 'r1')
    expect(await w.post({ run_id: 'r1' })).toEqual([200, {
      head: 'cancelled',
      tasks: { [`${P}/us-east1/jobs/nf-post`]: 'cancelled', [`${P}/us-east1/jobs/iv-merge`]: 'cancelled' },
      recorded: true,
    }])
    const filter = new URLSearchParams({ filter: jobsFilter('demo', '2026-11-19T1241'), pageSize: '200' })
    expect(w.calls).toEqual([
      { method: 'POST', url: `https://batch.googleapis.com/v1/${HEAD}:cancel`, body: REASON },
      { method: 'GET', url: `${LIST}?${filter}`, body: null },
      { method: 'POST', url: `https://batch.googleapis.com/v1/${P}/us-east1/jobs/nf-post:cancel`, body: REASON },
      { method: 'POST', url: `https://batch.googleapis.com/v1/${P}/us-east1/jobs/iv-merge:cancel`, body: REASON },
    ])
    expect(jobsFilter('demo', '2026-11-19T1241')).toBe('labels.deployment="demo" AND labels.scan_id="2026-11-19t1241"')
    expect(row(w.raw, 'r1')).toEqual([{ status: 'failed', error: `cancelled by ${ADMIN}`, finished_ts: NOW, updated_ts: NOW }])
  })

  it('reports a head already cancelling, or gone, and still cancels its tasks', async () => {
    const w = await world((u, m) => (m === 'GET' ? Response.json({ jobs: JOBS.slice(1, 2) }) : u.includes('snapshot') ? new Response('busy', { status: 409 }) : Response.json({})))
    run(w.raw, 'r1')
    expect(await w.post({ run_id: 'r1' })).toEqual([200, { head: 'already', tasks: { [`${P}/us-east1/jobs/nf-post`]: 'cancelled' }, recorded: true }])
  })

  it('refuses a run that isn’t a running scan, and leaves its row alone', async () => {
    const w = await world()
    run(w.raw, 'done', { status: 'succeeded' })
    run(w.raw, 'cmp', { kind: 'compact' })
    run(w.raw, 'rec', { parent: 'done' })
    run(w.raw, 'nojob', { job: null })
    expect(await w.post({ run_id: 'done' })).toEqual([409, { error: 'run done isn\'t running (succeeded)' }])
    expect(await w.post({ run_id: 'cmp' })).toEqual([409, { error: 'run cmp is a compact run, not a scan; only a scan run is cancelled here' }])
    expect(await w.post({ run_id: 'rec' })).toEqual([409, { error: 'run rec belongs to run done; cancel that one' }])
    expect(await w.post({ run_id: 'nojob' })).toEqual([409, { error: 'run nojob has no Batch job recorded' }])
    expect(await w.post({ run_id: 'nope' })).toEqual([404, { error: 'no run nope' }])
    expect(await w.post({})).toEqual([400, { error: 'run_id required' }])
    expect(w.calls).toEqual([])
    expect(row(w.raw, 'cmp')).toEqual([{ status: 'running', error: null, finished_ts: null, updated_ts: NOW - 60 }])
  })

  it('needs the deployment label: a bare scan id could match another deployment’s jobs', async () => {
    const w = await world(undefined, { DISKY_LABELS: 'app=disky' })
    run(w.raw, 'r1')
    expect(await w.post({ run_id: 'r1' })).toEqual([503, { error: 'scan cancel not configured (DISKY_LABELS deployment unset)' }])
    expect(w.calls).toEqual([])
  })
})
