// The deployment's GCP Batch settings for the sweep executors
// (specs/oa-decoupling.md step 5): which project the jobs run in, where, from
// which image, against which data bucket. All from `[vars]`; nothing here
// names a deployment. A route asks for the keys it needs and answers 503
// naming the first one unset.

/** The `[vars]` (and the `GCP_SA_KEY` secret) the executors read. */
export interface BatchEnv {
  /** The GCP project the Batch jobs, secrets and logs live in. Unset: the
   *  `project_id` of `GCP_SA_KEY`. */
  GCP_PROJECT?: string
  GCP_SA_KEY?: string
  /** The default Batch region (a job whose buckets span regions runs here). */
  BATCH_REGION?: string
  /** `{bucket: region}` as JSON: a one-region bucket cut runs its job there,
   *  beside the data. Unset: every job in `BATCH_REGION`. */
  BUCKET_REGIONS?: string
  /** The bucket a run's plan.json, manifest and logs land in. */
  DATA_BUCKET?: string
  /** This deployment's D1 (id + name), passed to executor jobs so they record their runs. */
  D1_DB_ID?: string
  D1_DB_NAME?: string
  /** The executor's container image. */
  SWEEP_IMAGE?: string
  /** The Cloudflare account the executor records runs to (its D1). */
  CF_ACCOUNT_ID?: string
  /** The S3 endpoint a plan-sweep run deletes through. */
  SWEEP_S3_ENDPOINT?: string
}

export interface BatchConfig {
  project: string
  region: string
  bucketRegions: Record<string, string>
  dataBucket: string
  image: string
  cfAccountId: string
  s3Endpoint: string
  /** The deployment's D1 (`sweep execute`/`undo` record their runs there). */
  d1DbId: string
  d1DbName: string
}

export type BatchKey = 'GCP_PROJECT' | 'DATA_BUCKET' | 'SWEEP_IMAGE' | 'CF_ACCOUNT_ID' | 'SWEEP_S3_ENDPOINT' | 'D1_DB_ID'

export const DEFAULT_BATCH_REGION = 'us-central1'

const projectOfKey = (saKey?: string): string => {
  if (!saKey) return ''
  try { return (JSON.parse(saKey) as { project_id?: string }).project_id ?? '' } catch { return '' }
}

/** The config, or the first of `need` that is unset. Keys not needed come back
 *  as ''. */
export function batchConfig(env: BatchEnv, need: readonly BatchKey[]): BatchConfig | { missing: BatchKey } {
  const cfg: BatchConfig = {
    project: env.GCP_PROJECT || projectOfKey(env.GCP_SA_KEY),
    region: env.BATCH_REGION || DEFAULT_BATCH_REGION,
    bucketRegions: env.BUCKET_REGIONS ? JSON.parse(env.BUCKET_REGIONS) as Record<string, string> : {},
    dataBucket: env.DATA_BUCKET ?? '',
    image: env.SWEEP_IMAGE ?? '',
    cfAccountId: env.CF_ACCOUNT_ID ?? '',
    s3Endpoint: env.SWEEP_S3_ENDPOINT ?? '',
    d1DbId: env.D1_DB_ID ?? '',
    d1DbName: env.D1_DB_NAME ?? '',
  }
  const field: Record<BatchKey, keyof BatchConfig> = {
    GCP_PROJECT: 'project', DATA_BUCKET: 'dataBucket', SWEEP_IMAGE: 'image', CF_ACCOUNT_ID: 'cfAccountId', SWEEP_S3_ENDPOINT: 's3Endpoint', D1_DB_ID: 'd1DbId',
  }
  const missing = need.find(k => !cfg[field[k]])
  return missing ? { missing } : cfg
}

/** The 503 message for an unset key: `<what> not configured (<KEY> unset)`. */
export const notConfigured = (what: string, key: string): string => `${what} not configured (${key} unset)`
