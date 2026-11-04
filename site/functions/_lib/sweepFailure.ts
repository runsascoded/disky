// Why a sweep job failed, in its own words: each step leaves `failure.json` in its output dir when it raises (`dt-cloud sweep
// manifest`, `execute`, `execute-reviewed`, `execute-manifest`). Batch's event only says "exit code 1", and a run refused before
// the executor records its start has no `deletion_runs` row to say more. A step killed from outside leaves no file.
import { NotFoundError, type GetResult } from '@rdub/file-tree'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { storeCreds, storeReady, storeTarget } from './index.js'

/** Bounds the read: the step truncates its reason well under this. */
const FAILURE_BYTES = 8192

type Get = (key: string, range: { offset: number; length: number }) => Promise<GetResult>

/** The job's recorded reason, or null: the run dir's (`execute*`, a dry run's `manifest`), else `source/`'s (a real run's `manifest`). */
export async function failureReason(jobId: string, get: Get): Promise<string | null> {
  for (const dir of [jobId, `${jobId}/source`]) {
    try {
      const { bytes } = await get(`sweep/runs/${dir}/failure.json`, { offset: 0, length: FAILURE_BYTES })
      const { reason } = JSON.parse(new TextDecoder().decode(bytes)) as { reason?: unknown }
      if (typeof reason === 'string' && reason) return reason
    } catch (error) {
      if (!(error instanceof NotFoundError)) throw error
    }
  }
  return null
}

export type FailureReader = (jobId: string) => Promise<string | null>

/** A reader over the deployment's store; never throws (a job's reason is worth having, not worth failing the listing for). */
export function failureReader(env: Parameters<typeof storeReady>[0]): FailureReader | undefined {
  if (!storeReady(env)) return undefined
  const store = S3Store({ ...storeTarget(env), ...storeCreds(env), prefixes: ['sweep/runs/'] })
  return jobId => failureReason(jobId, store.get).catch(error => {
    console.log(`failure reason of ${jobId}: ${(error as Error).message}`)
    return null
  })
}
