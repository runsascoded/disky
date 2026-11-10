import { describe, expect, it } from 'vitest'
import {
  checkedHistoryPlan, historyBatchSpec, historyConfig, historyJobId, historyJobs, historyJson, historyKey,
  HISTORY_OBJECT_BYTES, HISTORY_SCRIPT, HistoryError, type HistoryEnv, type HistoryPlan, type HistoryStorage,
  parseHistoryAction, reconcileHistoryJobs, requiredHistoryPins,
} from './historyAdmin'

const A = '2026-10-01T0000', B = '2026-10-02T0000', C = '2026-10-03T0000'
const JOB = 'history-8205900000000-12345678'
const PLAN: HistoryPlan = { digest: 'a'.repeat(64), scans: [A, B, C], retained: [A, C], removed: [B] }

export class HistoryBucket implements HistoryStorage {
  readonly values = new Map<string, unknown>()
  readonly writes: { key: string; value: unknown; options: unknown }[] = []
  readonly lists: unknown[] = []
  readonly gets: string[] = []
  page = { delimitedPrefixes: [`history/jobs/${JOB}/`], truncated: false, cursor: undefined as string | undefined }
  oversized: string | null = null
  async get(key: string) {
    this.gets.push(key)
    const value = this.values.get(key)
    return value === undefined ? null : { size: key === this.oversized ? HISTORY_OBJECT_BYTES + 1 : JSON.stringify(value).length,
      json: async <T>() => value as T }
  }
  async list(options: { prefix: string; delimiter: string; limit: number; cursor?: string }) {
    this.lists.push(options)
    return this.page
  }
  async put(key: string, body: string, options?: { onlyIf?: { etagDoesNotMatch: string }; httpMetadata?: { contentType: string } }) {
    if (options?.onlyIf?.etagDoesNotMatch === '*' && this.values.has(key)) return null
    const value: unknown = JSON.parse(body)
    this.values.set(key, value)
    this.writes.push({ key, value, options })
    return { etag: `etag-${this.writes.length}` }
  }
}

const ENV: HistoryEnv = {
  GCP_SA_KEY: '{}', JOB_SA: 'history-job@example.test', GCP_PROJECT: 'test-project', HISTORY_REGION: 'us-east1',
  HISTORY_IMAGE: 'repo/scan@sha256:abc', HISTORY_DATA_STORE: 'gs://data-bucket', HISTORY_SERVING_STORE: 'r2://serving-bucket',
  HISTORY_SECRETS: JSON.stringify({ R2_ENDPOINT: 'r2-endpoint', R2_ACCESS_KEY_ID: 'r2-key-id', R2_SECRET_ACCESS_KEY: 'r2-secret' }),
  DISKY_LABELS: 'app=disky,deployment=test',
}

describe('history input and required pins', () => {
  it('normalizes exact policy inputs and pins', () => {
    expect([
      parseHistoryAction({ action: 'plan', policy: { kind: 'low-change', drop_count: 4 }, pinned: [C, A] }),
      parseHistoryAction({ action: 'plan', policy: { kind: 'manual', remove: [B, A] }, pinned: [C] }),
      parseHistoryAction({ action: 'plan', policy: { kind: 'geometric', recent_seconds: 86400, growth: 2, samples_per_band: 3 } }),
      parseHistoryAction({ action: 'build', plan_id: JOB }),
    ]).toEqual([
      { action: 'plan', policy: { kind: 'low-change', drop_count: 4 }, pinned: [A, C] },
      { action: 'plan', policy: { kind: 'manual', remove: [A, B] }, pinned: [C] },
      { action: 'plan', policy: { kind: 'geometric', recent_seconds: 86400, growth: 2, samples_per_band: 3 }, pinned: [] },
      { action: 'build', plan_id: JOB },
    ])
  })

  it.each([
    [null, 'Expected an object'],
    [{ action: 'delete' }, 'action must be plan or build'],
    [{ action: 'build', plan_id: '../another/object' }, 'plan_id must name a history preview job'],
    [{ action: 'plan', policy: { kind: 'low-change', drop_count: 1 }, pinned: [A, A] }, 'pinned must be unique, complete scan IDs'],
    [{ action: 'plan', policy: { kind: 'low-change', drop_count: 0 } }, 'drop_count must be an integer from 1 to 10000'],
    [{ action: 'plan', policy: { kind: 'manual', remove: [] } }, 'remove must name at least one unique, complete scan ID'],
    [{ action: 'plan', policy: { kind: 'manual', remove: [A, A] } }, 'remove must name at least one unique, complete scan ID'],
    [{ action: 'plan', policy: { kind: 'manual', remove: ['2026-02-30'] } }, 'remove must name at least one unique, complete scan ID'],
    [{ action: 'plan', policy: { kind: 'off' } }, 'policy.kind must be manual, low-change or geometric'],
    [{ action: 'plan', policy: { kind: 'low-change', drop_count: 1.5 } }, 'drop_count must be an integer from 1 to 10000'],
    [{ action: 'plan', policy: { kind: 'geometric', recent_seconds: 0, growth: 2, samples_per_band: 1 } }, 'recent_seconds must be a positive integer, at most ten years'],
    [{ action: 'plan', policy: { kind: 'geometric', recent_seconds: 1, growth: 1, samples_per_band: 1 } }, 'growth must be greater than 1 and at most 100'],
    [{ action: 'plan', policy: { kind: 'geometric', recent_seconds: 1, growth: 2, samples_per_band: 0 } }, 'samples_per_band must be an integer from 1 to 100'],
  ])('refuses invalid input %#', (input, message) => {
    expect(() => parseHistoryAction(input)).toThrowError(message)
  })

  it('refuses incomplete previews, endpoint loss and new dependencies on removed scans', () => {
    expect(checkedHistoryPlan({ state: 'planned', plan: PLAN }, [A])).toEqual(PLAN)
    expect(() => checkedHistoryPlan({ state: 'running' }, [])).toThrowError('Preview is not complete; build a completed preview first')
    expect(() => checkedHistoryPlan({ state: 'planned', plan: { ...PLAN, retained: [A] } }, [])).toThrowError('Preview does not preserve its endpoints')
    expect(() => checkedHistoryPlan({ state: 'planned', plan: PLAN }, [B])).toThrowError(`Review or recovery now requires scans this preview would remove: ${B}. Build a new preview.`)
  })

  it('queries open staging and active deletion/recovery scans with the current clock, then canonicalizes them', async () => {
    const calls: unknown[] = []
    const statement = {
      bind: (...args: unknown[]) => { calls.push(args); return statement },
      all: async () => ({ results: [{ scan: C }, { scan: A }, { scan: C }] }),
    }
    const db = { prepare: (sql: string) => { calls.push(sql.replace(/\s+/g, ' ').trim()); return statement } }
    expect(await requiredHistoryPins(db as never, 1234567)).toEqual([A, C])
    expect(calls).toEqual([
      "SELECT DISTINCT i.as_of AS scan FROM plan_items i JOIN plans p ON p.id = i.plan_id WHERE p.state = 'open' AND i.as_of IS NOT NULL UNION SELECT DISTINCT scan FROM deletion_runs WHERE finished_ts IS NULL OR (undo_deadline > ? AND undo_state NOT IN ('full', 'expired'))",
      [1234],
    ])
  })

  it('refuses a malformed dependency instead of silently dropping its protection', async () => {
    const statement = {
      bind: () => statement,
      all: async () => ({ results: [{ scan: A }, { scan: 'not-a-scan' }] }),
    }
    const db = { prepare: () => statement }
    await expect(requiredHistoryPins(db as never, 1234567)).rejects.toStrictEqual(new HistoryError('History dependency contains an invalid scan ID: not-a-scan', 503))
  })
})

describe('history storage and Batch spec', () => {
  it('pages bounded records and reports a continuation cursor', async () => {
    const bucket = new HistoryBucket()
    bucket.page = { delimitedPrefixes: [`history/jobs/${JOB}/`, 'history/jobs/not-a-job/'], truncated: true, cursor: 'next' }
    bucket.values.set(historyKey(JOB, 'request'), { id: JOB })
    bucket.values.set(historyKey(JOB, 'status'), { state: 'planned', plan: PLAN })
    expect(await historyJobs(bucket, 'before')).toEqual({ jobs: [{ id: JOB, request: { id: JOB }, status: { state: 'planned', plan: PLAN } }], next_cursor: 'next' })
    expect(bucket.lists).toEqual([{ prefix: 'history/jobs/', delimiter: '/', limit: 20, cursor: 'before' }])
    expect(bucket.gets).toEqual([historyKey(JOB, 'request'), historyKey(JOB, 'status')])
  })

  it('bounds decoded record size and returns null for missing objects', async () => {
    const bucket = new HistoryBucket()
    expect(await historyJson(bucket, 'missing')).toBe(null)
    bucket.values.set('too-big', {})
    bucket.oversized = 'too-big'
    await expect(historyJson(bucket, 'too-big')).rejects.toThrowError('too-big: history record exceeds the size limit')
  })

  it('reports startup failure and missing completion receipts instead of leaving terminal Batch jobs queued', async () => {
    const bucket = new HistoryBucket(), job = { id: JOB, request: { id: JOB } as never, status: { state: 'queued' } }
    bucket.values.set(historyKey(JOB, 'status'), { state: 'queued' })
    expect(await reconcileHistoryJobs(bucket, [job], async () => 'FAILED')).toEqual([
      { ...job, status: { state: 'failed', batch_state: 'FAILED', error: 'Batch job failed. Open the Batch job for startup and task logs.' } },
    ])
    expect(await reconcileHistoryJobs(bucket, [job], async () => 'SUCCEEDED')).toEqual([
      { ...job, status: { state: 'failed', batch_state: 'SUCCEEDED', error: 'Batch job exited without a completed history result. No candidate is ready.' } },
    ])
    expect(bucket.writes).toEqual([])
  })

  it('prefers a just-completed worker receipt and exposes transient Batch API failures without fabricating a job failure', async () => {
    const bucket = new HistoryBucket(), job = { id: JOB, request: { id: JOB } as never, status: { state: 'queued' } }
    expect(await reconcileHistoryJobs(bucket, [job], async () => {
      bucket.values.set(historyKey(JOB, 'status'), { state: 'planned', plan: PLAN })
      return 'SUCCEEDED'
    })).toEqual([{ ...job, status: { state: 'planned', plan: PLAN, batch_state: 'SUCCEEDED' } }])
    expect(await reconcileHistoryJobs(bucket, [job], async () => { throw new Error('Batch unavailable (503)') })).toEqual([
      { ...job, status: { state: 'queued', batch_error: 'Batch unavailable (503)' } },
    ])
  })

  it('makes sortable job IDs and refuses incomplete configuration', () => {
    expect(historyJobId(1794099999999, '12345678-0000-0000-0000-000000000000')).toBe(JOB)
    expect(historyConfig({})).toEqual({ reason: 'History jobs are not configured (GCP_SA_KEY unset)' })
    expect(historyConfig({ ...ENV, HISTORY_SECRETS: '{}' })).toEqual({ reason: 'HISTORY_SECRETS must provide R2_ENDPOINT, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY' })
  })

  it('uses fixed shell source, configured secrets, bucket-local placement and enough local SSD for a 48 CPU build', () => {
    const cfg = historyConfig(ENV)
    if ('reason' in cfg) throw new Error(cfg.reason)
    const secrets = {
      R2_ENDPOINT: 'projects/test-project/secrets/r2-endpoint/versions/latest',
      R2_ACCESS_KEY_ID: 'projects/test-project/secrets/r2-key-id/versions/latest',
      R2_SECRET_ACCESS_KEY: 'projects/test-project/secrets/r2-secret/versions/latest',
    }
    expect(historyBatchSpec(cfg, JOB, 'build')).toEqual({
      taskGroups: [{ taskCount: 1, taskSpec: {
        runnables: [{ container: { imageUri: ENV.HISTORY_IMAGE, entrypoint: '/bin/bash', commands: ['-c', HISTORY_SCRIPT], volumes: ['/mnt/disks/stage:/work:rw'] } }],
        computeResource: { cpuMilli: 48000, memoryMib: 369600 }, maxRetryCount: 0, maxRunDuration: '86400s',
        volumes: [{ deviceName: 'stage', mountPath: '/mnt/disks/stage' }],
        environment: { variables: { HISTORY_JOB_ID: JOB, HISTORY_DATA_STORE: 'gs://data-bucket', HISTORY_SERVING_STORE: 'r2://serving-bucket', HISTORY_MEMORY: '300GB', HISTORY_THREADS: '40' }, secretVariables: secrets },
      } }],
      allocationPolicy: {
        instances: [{ policy: { machineType: 'n2-highmem-48', provisioningModel: 'STANDARD', bootDisk: { type: 'pd-balanced', sizeGb: '100' }, disks: [{ deviceName: 'stage', newDisk: { type: 'local-ssd', sizeGb: '3000' } }] } }],
        serviceAccount: { email: 'history-job@example.test' }, location: { allowedLocations: ['regions/us-east1'] },
        labels: { app: 'disky', deployment: 'test', component: 'history' },
      },
      logsPolicy: { destination: 'CLOUD_LOGGING' }, labels: { app: 'disky', deployment: 'test', component: 'history' },
    })
  })
})
