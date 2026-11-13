import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onRequestGet, onRequestPost } from './history'
import { gcpToken } from '../../_lib/gcp'
import { submitBatch } from '../../_lib/cwBatch'
import { historySourceScans } from '../../_lib/historySource.js'
import { type HistoryEnv, historyJobId, historyKey, type HistoryPlan } from '../../_lib/historyAdmin'

vi.mock('../../_lib/gcp', () => ({ gcpToken: vi.fn(), batchJobsUrl: () => 'https://batch.example.test' }))
vi.mock('../../_lib/cwBatch', () => ({ submitBatch: vi.fn() }))
vi.mock('../../_lib/historySource.js', () => ({ historySourceScans: vi.fn() }))

const A = '2026-10-01T0000', B = '2026-10-02T0000', C = '2026-10-03T0000'
const PREVIEW = 'history-8205900000000-12345678'
const PLAN: HistoryPlan = { digest: 'a'.repeat(64), scans: [A, B, C], retained: [A, C], removed: [B] }

function setup() {
  const values = new Map<string, unknown>(), writes: unknown[] = []
  const bucket = {
    list: vi.fn(async () => ({ delimitedPrefixes: [], truncated: false })),
    get: vi.fn(async (key: string) => {
      const value = values.get(key)
      return value === undefined ? null : { size: JSON.stringify(value).length, json: async () => value }
    }),
    put: vi.fn(async (key: string, body: string, options: { onlyIf?: unknown } = {}) => {
      if (options.onlyIf && values.has(key)) return null
      const value: unknown = JSON.parse(body)
      writes.push({ key, value, options })
      values.set(key, value)
      return { etag: 'written' }
    }),
  }
  const statement = { bind: vi.fn(() => statement), all: vi.fn(async () => ({ results: [{ scan: A }] })) }
  const env: HistoryEnv = {
    INDEX_R2: bucket as never, DB: { prepare: vi.fn(() => statement) } as never,
    GCP_SA_KEY: '{}', JOB_SA: 'job@example.test', GCP_PROJECT: 'test-project', HISTORY_REGION: 'us-east1',
    HISTORY_IMAGE: 'repo/scan@sha256:abc', HISTORY_DATA_STORE: 'gs://data-bucket', HISTORY_SERVING_STORE: 'r2://serving-bucket',
    HISTORY_SECRETS: JSON.stringify({ R2_ENDPOINT: 'r2-endpoint', R2_ACCESS_KEY_ID: 'r2-key-id', R2_SECRET_ACCESS_KEY: 'r2-secret' }),
  }
  const ctx = (body?: unknown, host = 'localhost') => ({ env,
    request: new Request(`http://${host}/api/admin/history`, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json' } }),
  })
  return { env, values, writes, bucket, statement, ctx }
}

afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })
beforeEach(() => { vi.mocked(historySourceScans).mockResolvedValue([A, B, C]) })

describe('/api/admin/history', () => {
  it('gates reads and writes before touching storage or dispatch', async () => {
    const f = setup()
    const responses = await Promise.all([onRequestGet(f.ctx(undefined, 'example.test')), onRequestPost(f.ctx({}, 'example.test'))])
    expect(responses.map(r => r.status)).toEqual([401, 401])
    expect([f.bucket.list.mock.calls, f.bucket.get.mock.calls, f.bucket.put.mock.calls, vi.mocked(submitBatch).mock.calls, vi.mocked(historySourceScans).mock.calls]).toEqual([[], [], [], [], []])
  })

  it('keeps the writer roster and protected pins visible while dispatch credentials are unavailable', async () => {
    const f = setup()
    f.env.GCP_SA_KEY = undefined
    const get = await onRequestGet(f.ctx()), post = await onRequestPost(f.ctx({ action: 'plan' }))
    expect([get.status, get.headers.get('cache-control'), await get.json(), post.status, await post.json()]).toEqual([
      200, 'no-store', { configured: true, enabled: false, reason: 'History jobs are not configured (GCP_SA_KEY unset)', source_scans: [A, B, C], required_pins: [A], jobs: [] },
      503, { error: 'History jobs are not configured (GCP_SA_KEY unset)' },
    ])
    expect(f.writes).toEqual([])
    expect(vi.mocked(historySourceScans).mock.calls).toEqual([[f.env]])
    expect(vi.mocked(gcpToken).mock.calls).toEqual([])
  })

  it('reads nothing, and says so, on a deployment without history', async () => {
    const f = setup()
    for (const key of ['HISTORY_IMAGE', 'HISTORY_DATA_STORE', 'HISTORY_SERVING_STORE', 'HISTORY_REGION', 'HISTORY_SECRETS'] as const) f.env[key] = undefined
    const get = await onRequestGet(f.ctx()), probe = await onRequestGet({ env: f.env, request: new Request('http://localhost/api/admin/history?configured') })
    expect([get.status, await get.json(), probe.status, await probe.json()]).toEqual([
      200, { configured: false, enabled: false, reason: 'Scan history is not configured', jobs: [] },
      200, { configured: false, enabled: false, reason: 'Scan history is not configured', jobs: [] },
    ])
    expect([f.bucket.list.mock.calls, f.bucket.get.mock.calls, vi.mocked(f.env.DB!.prepare).mock.calls, vi.mocked(historySourceScans).mock.calls]).toEqual([[], [], [], []])
  })

  it('answers the configured probe alone: a version pointer is enough', async () => {
    const f = setup()
    for (const key of ['HISTORY_IMAGE', 'HISTORY_DATA_STORE', 'HISTORY_SERVING_STORE', 'HISTORY_REGION', 'HISTORY_SECRETS'] as const) f.env[key] = undefined
    f.env.HISTORY_CURRENT_KEY = 'history/environments/dev/CURRENT.json'
    const probe = await onRequestGet({ env: f.env, request: new Request('http://localhost/api/admin/history?configured') })
    expect([probe.status, await probe.json()]).toEqual([200, { configured: true }])
    expect([f.bucket.list.mock.calls, vi.mocked(historySourceScans).mock.calls]).toEqual([[], []])
  })

  it('records an immutable review request and queued status before submitting one Batch job', async () => {
    const f = setup(), now = 1794099999999
    vi.spyOn(Date, 'now').mockReturnValue(now)
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('12345678-0000-0000-0000-000000000000')
    vi.mocked(gcpToken).mockResolvedValue('test-token')
    vi.mocked(submitBatch).mockResolvedValue({ ok: true, status: 200, text: '{}' })
    const response = await onRequestPost(f.ctx({ action: 'plan', policy: { kind: 'low-change', drop_count: 1 }, pinned: [C] }))
    const id = historyJobId(now, '12345678-0000-0000-0000-000000000000')
    const request = {
      version: 1, id, action: 'plan', created_at: new Date(now).toISOString(), actor: 'dev@example.test',
      policy: { kind: 'low-change', drop_count: 1 }, pinned: [C], required_pins: [A],
      data_store: 'gs://data-bucket', serving_store: 'r2://serving-bucket',
      batch_url: `https://console.cloud.google.com/batch/jobsDetail/regions/us-east1/jobs/${id}?project=test-project`,
    }
    expect([response.status, response.headers.get('cache-control'), await response.json()]).toEqual([202, 'no-store', { id, request, status: { state: 'queued' } }])
    expect(f.writes).toEqual([
      { key: historyKey(id, 'request'), value: request, options: { httpMetadata: { contentType: 'application/json' }, onlyIf: { etagDoesNotMatch: '*' } } },
      { key: historyKey(id, 'status'), value: { state: 'queued', updated_at: request.created_at }, options: { httpMetadata: { contentType: 'application/json' } } },
    ])
    expect(vi.mocked(gcpToken).mock.calls).toEqual([['{}']])
    expect(vi.mocked(submitBatch).mock.calls.map(([cfg, token, job]) => ({ project: cfg.project, region: cfg.region, token, job }))).toEqual([
      { project: 'test-project', region: 'us-east1', token: 'test-token', job: id },
    ])
  })

  it('refuses a build when a newly staged review needs a removed scan', async () => {
    const f = setup()
    f.statement.all.mockResolvedValue({ results: [{ scan: B }] })
    f.values.set(historyKey(PREVIEW, 'status'), { state: 'planned', plan: PLAN })
    const response = await onRequestPost(f.ctx({ action: 'build', plan_id: PREVIEW }))
    expect([response.status, await response.json(), f.writes, vi.mocked(submitBatch).mock.calls]).toEqual([
      409, { error: `Review or recovery now requires scans this preview would remove: ${B}. Build a new preview.` }, [], [],
    ])
  })

  it('reports Batch rejection as a failed job without changing the immutable request', async () => {
    const f = setup()
    vi.mocked(gcpToken).mockResolvedValue('test-token')
    vi.mocked(submitBatch).mockResolvedValue({ ok: false, status: 400, text: 'invalid resource configuration' })
    const response = await onRequestPost(f.ctx({ action: 'plan', policy: { kind: 'low-change', drop_count: 1 } }))
    const body = await response.json() as { id: string; error: string; batch_url: string }
    expect([response.status, body.error]).toEqual([502, 'Batch rejected the job (400): invalid resource configuration'])
    const status = f.values.get(historyKey(body.id, 'status')) as { state: string; error: string; updated_at: string }
    expect({ state: status.state, error: status.error }).toEqual({ state: 'failed', error: body.error })
    expect(f.writes.filter(row => (row as { key: string }).key === historyKey(body.id, 'request')).length).toBe(1)
  })
})
