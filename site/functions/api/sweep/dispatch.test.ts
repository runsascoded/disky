// `POST /api/sweep/dispatch`: `shown_min_soft_delete_days`, the minimum whoever confirmed was shown. A real run refuses a lower
// current minimum, and a request naming none counts as having been shown 7.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'
import { onRequestPost } from './dispatch'

vi.mock('../../_lib/gcp.js', async original => ({ ...await original<typeof import('../../_lib/gcp.js')>(), gcpToken: async () => 'token' }))

const BUCKET = 'gcs-a'
const PREFIX = `gs://${BUCKET}/a/`
let env: Record<string, unknown>
let token: string
let submitted: number

beforeEach(async () => {
  const { db, raw } = await sqliteD1()
  env = {
    DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', EXECUTOR: 'sweep',
    GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'project', DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'image@sha256:abc', CF_ACCOUNT_ID: 'account', D1_DB_ID: 'db',
    STORE_SCHEME: 'gs://', STORE_BUCKETS: BUCKET, BUCKET_REGIONS: JSON.stringify({ [BUCKET]: 'us-east5' }),
  }
  token = (await gateFor(env as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'ops@example.test', 1)")
  raw.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ops@example.test', 1)").run(PREFIX)
  raw.exec("INSERT INTO sweep_settings (id, min_soft_delete_days, updated_by, updated_at) VALUES (1, 0, 'ops', 1)")
  submitted = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/jobs?job_id=')) submitted++
    return new Response('{}', { status: 200 })
  }))
})
afterEach(() => vi.unstubAllGlobals())

const post = async (extra: Record<string, unknown>) => {
  const r = await onRequestPost({
    request: new Request('https://site.example.test/api/sweep/dispatch', {
      method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ plan_id: 1, mode: 'real', date: '2026-11-04', items: [PREFIX], ...extra }),
    }),
    env: env as never,
  })
  return { status: r.status, body: await r.json() as Record<string, unknown>, submitted }
}

const REFUSED = {
  error: 'not deleting: the soft-delete minimum is now 0 days, lower than the 7 days these terms promised, so the undo they describe may not exist. Re-read the terms and confirm again.',
  min_soft_delete_days: 0,
  shown_min_soft_delete_days: 7,
}

describe('shown_min_soft_delete_days', () => {
  it('a tab still showing 7 is refused at 0', async () => {
    expect(await post({ shown_min_soft_delete_days: 7 })).toEqual({ status: 409, body: REFUSED, submitted: 0 })
  })
  it('a request naming none counts as 7', async () => {
    expect(await post({})).toEqual({ status: 409, body: REFUSED, submitted: 0 })
  })
  it('naming what is current runs', async () => {
    const { status, submitted: jobs, body } = await post({ shown_min_soft_delete_days: 0 })
    expect([status, jobs, body.min_soft_delete_days]).toEqual([200, 1, 0])
  })
  it('a dry run ignores it', async () => {
    const { status, submitted: jobs } = await post({ mode: 'dry' })
    expect([status, jobs]).toEqual([200, 1])
  })
  it.each([[-1], [91], [1.5], ['7'], [null]])('%j is a 400', async shown => {
    expect(await post({ shown_min_soft_delete_days: shown })).toEqual({ status: 400, body: { error: 'shown_min_soft_delete_days: an integer 0..90' }, submitted: 0 })
  })
})
