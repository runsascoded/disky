/** Admin-only, asynchronous history planning and candidate builds. Requests are immutable R2 objects;
 * the Batch worker writes status beside them. Dispatch never moves a serving pointer or deletes history. */
import type { D1Database } from '@cloudflare/workers-types'
import type { Env } from './auth.js'
import type { BatchConfig, BatchEnv } from './batchConfig.js'
import { labelBatchSpec, parseLabels } from './costLabels.js'
import { isScanId } from '../../src/scanSlug.js'

export interface HistoryEnv extends Env, BatchEnv {
  HISTORY_IMAGE?: string
  HISTORY_DATA_STORE?: string
  HISTORY_SERVING_STORE?: string
  HISTORY_REGION?: string
  HISTORY_SECRETS?: string
}

export type HistoryPolicy = { kind: 'low-change'; drop_count: number }
  | { kind: 'geometric'; recent_seconds: number; growth: number; samples_per_band: number }
  | { kind: 'manual'; remove: string[] }
export type HistoryAction = { action: 'plan'; policy: HistoryPolicy; pinned: string[] }
  | { action: 'build'; plan_id: string }
export interface HistoryMetric { count: number; bytes?: number | null }
export interface HistoryStep {
  scan: string; before: string; after: string; score: number
  left: HistoryMetric; right: HistoryMetric; merged: HistoryMetric
}
export interface HistoryCandidateScore {
  scan: string
  before: string | null
  after: string | null
  left: HistoryMetric | null
  right: HistoryMetric | null
  score: number | null
  protected: boolean
  reasons: string[]
}
export interface HistorySavingsEstimate {
  method: 'projected-rows-proportional-compressed-data'
  scope: 'folded-interval-canonical-tables'
  source_bytes: number
  estimated_retained_bytes: number
  estimated_saved_bytes: number
  source_rows: number
  retained_rows: number
  exact_rows: true
  estimated_bytes: true
  tables: Record<string, unknown>
  exclusions: string[]
}
export interface HistoryMeasuredCopy {
  source_bytes: number
  candidate_bytes: number
  source_files: number
  candidate_files: number
  additional_generation_bytes_before_gc: number
  net_reduction_after_source_gc_bytes: number
}
export interface HistoryMeasuredStorage {
  method: 'measured-generation-inventories'
  scope: 'source-and-candidate-generation-copies'
  source_bytes: number
  candidate_bytes: number
  additional_generation_bytes_before_gc: number
  net_reduction_after_source_gc_bytes: number
  potential_source_reclaim_bytes: number
  copies: Record<string, HistoryMeasuredCopy>
  reclaim_condition: string
  exclusions: string[]
}
export interface HistoryCandidate extends Record<string, unknown> {
  version?: string
  id?: string
  storage?: HistoryMeasuredStorage
  descriptor?: { storage?: HistoryMeasuredStorage; [key: string]: unknown }
}
export interface HistoryPlan {
  digest: string
  scans: string[]
  retained: string[]
  removed?: string[]
  pinned?: string[]
  required_pins?: string[]
  steps?: HistoryStep[]
  candidates?: HistoryCandidateScore[]
  estimate?: HistorySavingsEstimate
  source?: Record<string, unknown>
  churn?: string
}
export interface HistoryRequest {
  version: 1
  id: string
  action: 'plan' | 'build'
  created_at: string
  actor: string
  policy?: HistoryPolicy
  pinned?: string[]
  required_pins: string[]
  plan_id?: string
  plan?: HistoryPlan
  data_store: string
  serving_store: string
  batch_url: string
}
export interface HistoryStatus {
  state: string
  phase?: string
  error?: string
  note?: string
  plan?: HistoryPlan
  candidate?: HistoryCandidate
  updated_at?: string
  batch_state?: string
  batch_error?: string
}
export interface HistoryJob { id: string; request: HistoryRequest; status: HistoryStatus | null }
export interface HistoryData {
  enabled: boolean
  reason?: string
  jobs: HistoryJob[]
  next_cursor?: string
  source_scans?: string[]
  required_pins?: string[]
}

export const HISTORY_PREFIX = 'history/jobs/'
export const HISTORY_JOB_RE = /^history-\d{13}-[a-f0-9]{8}$/
export const HISTORY_PAGE_SIZE = 20
export const HISTORY_OBJECT_BYTES = 1 << 20
const FINISHED = new Set(['planned', 'built', 'failed'])
export const unfinishedHistoryJob = (job: HistoryJob): boolean => !FINISHED.has(job.status?.state ?? 'queued')

export class HistoryError extends Error {
  constructor(message: string, readonly status = 400) { super(message) }
}

/** Bound JSON request bodies even when Content-Length is absent. */
export async function historyRequestBody(request: Request): Promise<unknown> {
  if (!request.body) throw new HistoryError('A JSON request body is required')
  const reader = request.body.getReader(), decoder = new TextDecoder()
  let text = '', bytes = 0
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    bytes += value.byteLength
    if (bytes > 65536) { await reader.cancel(); throw new HistoryError('Request body exceeds 64 KiB', 413) }
    text += decoder.decode(value, { stream: true })
  }
  text += decoder.decode()
  try { return JSON.parse(text) } catch { throw new HistoryError('Invalid JSON') }
}

/** Small storage surface shared by the Worker binding and precise in-memory tests. */
export interface HistoryStorage {
  get(key: string): Promise<{ size: number; json<T>(): Promise<T> } | null>
  list(options: { prefix: string; delimiter: string; limit: number; cursor?: string }): Promise<{
    delimitedPrefixes: string[]; truncated: boolean; cursor?: string
  }>
  put(key: string, body: string, options?: { onlyIf?: { etagDoesNotMatch: string }; httpMetadata?: { contentType: string } }): Promise<unknown>
}

export const historyKey = (id: string, file: 'request' | 'status'): string => `${HISTORY_PREFIX}${id}/${file}.json`
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const integer = (v: unknown, min: number, max: number): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max
const ids = (v: unknown): v is string[] => Array.isArray(v) && v.length <= 10_000 && v.every(isScanId) && new Set(v).size === v.length

export function parseHistoryAction(v: unknown): HistoryAction {
  if (!record(v)) throw new HistoryError('Expected an object')
  if (v.action === 'build') {
    if (typeof v.plan_id !== 'string' || !HISTORY_JOB_RE.test(v.plan_id)) throw new HistoryError('plan_id must name a history preview job')
    return { action: 'build', plan_id: v.plan_id }
  }
  if (v.action !== 'plan' || !record(v.policy)) throw new HistoryError('action must be plan or build')
  const pinned = v.pinned ?? []
  if (!ids(pinned)) throw new HistoryError('pinned must be unique, complete scan IDs')
  const p = v.policy
  if (p.kind === 'manual') {
    if (!ids(p.remove) || !p.remove.length) throw new HistoryError('remove must name at least one unique, complete scan ID')
    return { action: 'plan', policy: { kind: 'manual', remove: [...p.remove].sort() }, pinned: [...pinned].sort() }
  }
  if (p.kind === 'low-change') {
    if (!integer(p.drop_count, 1, 10_000)) throw new HistoryError('drop_count must be an integer from 1 to 10000')
    return { action: 'plan', policy: { kind: 'low-change', drop_count: p.drop_count }, pinned: [...pinned].sort() }
  }
  if (p.kind === 'geometric') {
    if (!integer(p.recent_seconds, 1, 10 * 366 * 86400)) throw new HistoryError('recent_seconds must be a positive integer, at most ten years')
    if (typeof p.growth !== 'number' || !Number.isFinite(p.growth) || p.growth <= 1 || p.growth > 100) throw new HistoryError('growth must be greater than 1 and at most 100')
    if (!integer(p.samples_per_band, 1, 100)) throw new HistoryError('samples_per_band must be an integer from 1 to 100')
    return { action: 'plan', policy: { kind: 'geometric', recent_seconds: p.recent_seconds, growth: p.growth, samples_per_band: p.samples_per_band }, pinned: [...pinned].sort() }
  }
  throw new HistoryError('policy.kind must be manual, low-change or geometric')
}

export async function historyJson<T>(bucket: HistoryStorage, key: string): Promise<T | null> {
  const got = await bucket.get(key)
  if (!got) return null
  if (got.size > HISTORY_OBJECT_BYTES) throw new HistoryError(`${key}: history record exceeds the size limit`, 503)
  return got.json<T>()
}

/** IDs sort newest first because the numeric component is the complement of their creation time. */
export const historyJobId = (now: number, uuid: string): string => `history-${String(9_999_999_999_999 - now).padStart(13, '0')}-${uuid.replaceAll('-', '').slice(0, 8)}`

export async function historyJobs(bucket: HistoryStorage, cursor?: string): Promise<Pick<HistoryData, 'jobs' | 'next_cursor'>> {
  if (cursor && cursor.length > 2048) throw new HistoryError('Invalid cursor')
  const page = await bucket.list({ prefix: HISTORY_PREFIX, delimiter: '/', limit: HISTORY_PAGE_SIZE, ...(cursor ? { cursor } : {}) })
  const names = page.delimitedPrefixes.map(p => p.slice(HISTORY_PREFIX.length).replace(/\/$/, '')).filter(id => HISTORY_JOB_RE.test(id))
  const jobs: HistoryJob[] = []
  // Bound simultaneous decoded records as well as the number of jobs in a page.
  for (let i = 0; i < names.length; i += 4) {
    const rows = await Promise.all(names.slice(i, i + 4).map(async id => {
      const [request, status] = await Promise.all([
        historyJson<HistoryRequest>(bucket, historyKey(id, 'request')),
        historyJson<HistoryStatus>(bucket, historyKey(id, 'status')),
      ])
      return request ? { id, request, status } : null
    }))
    jobs.push(...rows.filter((row): row is HistoryJob => row !== null))
  }
  return { jobs, ...(page.truncated && page.cursor ? { next_cursor: page.cursor } : {}) }
}

/** Startup failures have no worker receipt. Reconcile terminal Batch state while treating API failures as unknown.
 * Re-read the worker receipt after Batch: a just-finished worker's completion always wins over the earlier page read.
 * Derived failure states are returned to the console without overwriting worker-owned status objects. */
export async function reconcileHistoryJobs(bucket: HistoryStorage, jobs: HistoryJob[], readState: (id: string) => Promise<string>): Promise<HistoryJob[]> {
  const result: HistoryJob[] = []
  for (let i = 0; i < jobs.length; i += 4) {
    result.push(...await Promise.all(jobs.slice(i, i + 4).map(async job => {
      if (!unfinishedHistoryJob(job)) return job
      let batchState: string
      try { batchState = await readState(job.id) } catch (e) {
        return { ...job, status: { ...job.status, state: job.status?.state ?? 'queued', batch_error: e instanceof Error ? e.message : String(e) } }
      }
      const fresh = await historyJson<HistoryStatus>(bucket, historyKey(job.id, 'status'))
      if (fresh && FINISHED.has(fresh.state)) return { ...job, status: { ...fresh, batch_state: batchState } }
      if (batchState === 'FAILED' || batchState === 'SUCCEEDED') {
        return { ...job, status: { ...fresh, state: 'failed', batch_state: batchState, error: batchState === 'FAILED'
          ? 'Batch job failed. Open the Batch job for startup and task logs.'
          : 'Batch job exited without a completed history result. No candidate is ready.' } }
      }
      return { ...job, status: { ...(fresh ?? job.status), state: fresh?.state ?? job.status?.state ?? 'queued', batch_state: batchState } }
    })))
  }
  return result
}

/** Pending reviews and deletion/recovery work must continue to resolve the snapshots they depend on. */
export async function requiredHistoryPins(db: D1Database, now: number): Promise<string[]> {
  const got = await db.prepare(`
    SELECT DISTINCT i.as_of AS scan FROM plan_items i JOIN plans p ON p.id = i.plan_id
      WHERE p.state = 'open' AND i.as_of IS NOT NULL
    UNION SELECT DISTINCT scan FROM deletion_runs
      WHERE finished_ts IS NULL OR (undo_deadline > ? AND undo_state NOT IN ('full', 'expired'))
  `).bind(Math.floor(now / 1000)).all<{ scan: string }>()
  for (const row of got.results) if (!isScanId(row.scan)) throw new HistoryError(`History dependency contains an invalid scan ID: ${String(row.scan)}`, 503)
  return [...new Set(got.results.map(row => row.scan))].sort()
}

export function checkedHistoryPlan(status: HistoryStatus | null, requiredPins: string[]): HistoryPlan {
  const plan = status?.plan
  if (status?.state !== 'planned' || !plan || !/^[a-f0-9]{64}$/.test(plan.digest)) throw new HistoryError('Preview is not complete; build a completed preview first', 409)
  if (!ids(plan.scans) || !ids(plan.retained) || plan.scans.length < 2) throw new HistoryError('Preview has an invalid scan roster', 409)
  const roster = new Set(plan.scans), kept = new Set(plan.retained)
  if (plan.retained.some(id => !roster.has(id)) || !kept.has(plan.scans[0]) || !kept.has(plan.scans.at(-1)!)) throw new HistoryError('Preview does not preserve its endpoints', 409)
  const droppedPins = requiredPins.filter(id => !kept.has(id))
  if (droppedPins.length) throw new HistoryError(`Review or recovery now requires scans this preview would remove: ${droppedPins.join(', ')}. Build a new preview.`, 409)
  return plan
}

interface HistoryConfig {
  batch: BatchConfig
  serviceAccount: string
  dataStore: string
  servingStore: string
  secrets: Record<string, string>
}

export function historyConfig(env: HistoryEnv): HistoryConfig | { reason: string } {
  for (const key of ['GCP_SA_KEY', 'JOB_SA', 'GCP_PROJECT', 'HISTORY_IMAGE', 'HISTORY_DATA_STORE', 'HISTORY_SERVING_STORE', 'HISTORY_REGION', 'HISTORY_SECRETS'] as const) {
    if (!env[key]?.trim()) return { reason: `History jobs are not configured (${key} unset)` }
  }
  const dataStore = env.HISTORY_DATA_STORE!, servingStore = env.HISTORY_SERVING_STORE!
  if (!/^gs:\/\/[^/]+(?:\/[^?#]*)?$/.test(dataStore) || !/^r2:\/\/[^/]+(?:\/[^?#]*)?$/.test(servingStore)) return { reason: 'History jobs require a GCS data store and R2 serving store' }
  if (!/^[a-z]+-[a-z]+\d+$/.test(env.HISTORY_REGION!)) return { reason: 'HISTORY_REGION must name the data bucket’s region' }
  let bindings: unknown
  try { bindings = JSON.parse(env.HISTORY_SECRETS!) } catch { return { reason: 'HISTORY_SECRETS must be an object of environment variable to Secret Manager name' } }
  if (!record(bindings)) return { reason: 'HISTORY_SECRETS must be an object of environment variable to Secret Manager name' }
  const secrets: Record<string, string> = {}
  for (const [key, name] of Object.entries(bindings)) {
    if (!/^[A-Z][A-Z0-9_]*$/.test(key) || typeof name !== 'string' || !/^[\w-]+$/.test(name)) return { reason: 'HISTORY_SECRETS contains an invalid binding' }
    secrets[key] = `projects/${env.GCP_PROJECT}/secrets/${name}/versions/latest`
  }
  if (!['R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'].every(key => secrets[key])) return { reason: 'HISTORY_SECRETS must provide R2_ENDPOINT, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY' }
  return {
    batch: { project: env.GCP_PROJECT!, region: env.HISTORY_REGION!, image: env.HISTORY_IMAGE!, dataBucket: new URL(dataStore).hostname,
      bucketRegions: {}, cfAccountId: env.CF_ACCOUNT_ID ?? '', s3Endpoint: '', d1DbId: env.D1_DB_ID ?? '', d1DbName: env.D1_DB_NAME ?? '', labels: parseLabels(env.DISKY_LABELS) },
    serviceAccount: env.JOB_SA!, dataStore, servingStore, secrets,
  }
}

/** All variable input enters as environment values, never as shell source. The worker owns status updates. */
export const HISTORY_SCRIPT = [
  'set -euo pipefail',
  'export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"',
  'export DISKY_R2_ENDPOINT_URL="$R2_ENDPOINT" AWS_EC2_METADATA_DISABLED=true',
  'unset AWS_SESSION_TOKEN AWS_PROFILE',
  'dt-cloud history run -r "$HISTORY_SERVING_STORE" -i "$HISTORY_JOB_ID" -w /work/history -m "$HISTORY_MEMORY" -t "$HISTORY_THREADS"',
].join('\n')

export function historyBatchSpec(cfg: HistoryConfig, id: string, action: 'plan' | 'build'): unknown {
  if (!HISTORY_JOB_RE.test(id)) throw new HistoryError('Invalid job ID')
  const build = action === 'build', cpus = build ? 48 : 8
  return labelBatchSpec({
    taskGroups: [{ taskCount: 1, taskSpec: {
      runnables: [{ container: { imageUri: cfg.batch.image, entrypoint: '/bin/bash', commands: ['-c', HISTORY_SCRIPT], volumes: ['/mnt/disks/stage:/work:rw'] } }],
      computeResource: { cpuMilli: cpus * 1000, memoryMib: cpus * 7700 },
      maxRetryCount: 0, maxRunDuration: '86400s',
      volumes: [{ deviceName: 'stage', mountPath: '/mnt/disks/stage' }],
      environment: { variables: { HISTORY_JOB_ID: id, HISTORY_DATA_STORE: cfg.dataStore, HISTORY_SERVING_STORE: cfg.servingStore,
        HISTORY_MEMORY: build ? '300GB' : '20GB', HISTORY_THREADS: build ? '40' : '8' }, secretVariables: cfg.secrets },
    } }],
    allocationPolicy: {
      instances: [{ policy: { machineType: `n2-highmem-${cpus}`, provisioningModel: 'STANDARD', bootDisk: { type: 'pd-balanced', sizeGb: '100' },
        disks: [{ deviceName: 'stage', newDisk: { type: 'local-ssd', sizeGb: build ? '3000' : '375' } }] } }],
      serviceAccount: { email: cfg.serviceAccount }, location: { allowedLocations: [`regions/${cfg.batch.region}`] },
    },
    logsPolicy: { destination: 'CLOUD_LOGGING' },
  }, cfg.batch.labels, 'history')
}
