// What a finished sweep's Slack message adds to its D1 totals, from the run dir's `plan-summary.json`: the same file and the same
// reading (`attemptOnly`, `skipped_no_generation`) as its `/staged` row. D1 has no column for either.
import { NotFoundError, type GetResult } from '@rdub/file-tree'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type PlanSummaryFile, runOutcome, type RunOutcome } from '../../src/runs.js'
import { storeCreds, storeReady, storeTarget } from './index.js'

type Get = (key: string) => Promise<Pick<GetResult, 'bytes'>>

const isCount = (n: unknown): boolean => typeof n === 'number' && Number.isFinite(n)

/** The job's outcome; null: nothing to add (an older run), or no file. Throws on a failed read or a document it can't read. */
export async function runOutcomeOf(jobId: string, get: Get): Promise<RunOutcome | null> {
  let bytes: Uint8Array
  try {
    ({ bytes } = await get(`sweep/runs/${jobId}/plan-summary.json`))
  } catch (error) {
    if (error instanceof NotFoundError) return null
    throw error
  }
  const summary = JSON.parse(new TextDecoder().decode(bytes)) as PlanSummaryFile
  const skips = summary.skipped_no_generation
  if (skips && !(isCount(skips.objects) && isCount(skips.bytes) && Array.isArray(skips.whole_buckets))) throw new Error('malformed skipped_no_generation')
  return runOutcome(summary)
}

export type OutcomeReader = (jobId: string) => Promise<RunOutcome | null>

/** Never throws: the announcement goes out without it, and the D1 reflection it follows has already landed. */
export const outcomeReaderOf = (get: Get): OutcomeReader => jobId => runOutcomeOf(jobId, get).catch(error => {
  console.log(`outcome of ${jobId}: ${(error as Error).message}`)
  return null
})

/** A reader over the deployment's store. */
export function outcomeReader(env: Parameters<typeof storeReady>[0]): OutcomeReader | undefined {
  if (!storeReady(env)) return undefined
  const store = S3Store({ ...storeTarget(env), ...storeCreds(env), prefixes: ['sweep/runs/'] })
  return outcomeReaderOf(key => store.get(key))
}
