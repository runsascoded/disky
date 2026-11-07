// What a finished sweep's Slack message adds to its D1 totals, from the run dir's `plan-summary.json`: the same file and the same
// reading (`attemptOnly`, `skipped_no_generation`) as its `/staged` row. D1 has no column for either.
import { NotFoundError, type GetResult } from '@rdub/file-tree'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type PlanSummaryFile, runJobId, runOutcome, type RunOutcome } from '../../src/runs.js'
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

/** A failed read: nothing says whether this run's totals are deletions or attempts. */
export const UNREAD: RunOutcome = { attempted: false, skipped: null, notSwept: [], unread: true }

/** Never throws: the announcement goes out saying its totals are unread, and the D1 reflection it follows has already landed. */
export const outcomeReaderOf = (get: Get): OutcomeReader => jobId => runOutcomeOf(jobId, get).catch(error => {
  console.log(`outcome of ${jobId}: ${(error as Error).message}`)
  return UNREAD
})

/** A reader over the deployment's store. */
export function outcomeReader(env: Parameters<typeof storeReady>[0]): OutcomeReader | undefined {
  if (!storeReady(env)) return undefined
  const store = S3Store({ ...storeTarget(env), ...storeCreds(env), prefixes: ['sweep/runs/'] })
  return outcomeReaderOf(key => store.get(key))
}

interface LastRun { run_id: string; plan_digest: string | null; log_dir?: string | null }

/** What the plan card says of its last real run, re-rendered on every event with no announcement to carry it: its contract and
 * skips from `plan-summary.json`, and, if it ended unsuccessfully, whether a final summary backs its totals (one byte's read).
 * Null: nothing to add, or not a sweep job's run dir. Never throws. */
export async function lastRunOutcomeOf(r: LastRun, get: (key: string, range?: { offset: number; length: number }) => Promise<Pick<GetResult, 'bytes'>>): Promise<RunOutcome | null> {
  if (!r.log_dir) return null
  const jobId = runJobId({ run_id: r.run_id, log_dir: r.log_dir })
  if (!/^gcs-sweep-(dry|real)-/.test(jobId)) return null
  try {
    const outcome = await runOutcomeOf(jobId, get)
    if (r.plan_digest !== '') return outcome
    const summary = await get(`sweep/runs/${jobId}/deleted-summary.json`, { offset: 0, length: 1 }).catch(error => {
      if (error instanceof NotFoundError) return null
      throw error
    })
    return summary ? outcome : { ...(outcome ?? { attempted: false, skipped: null, notSwept: [] }), totals: 'lower-bound' }
  } catch (error) {
    console.log(`last outcome of ${jobId}: ${(error as Error).message}`)
    return UNREAD
  }
}

/** A reader over the deployment's store. */
export function lastRunOutcome(env: Parameters<typeof storeReady>[0]): ((r: LastRun) => Promise<RunOutcome | null>) | undefined {
  if (!storeReady(env)) return undefined
  const store = S3Store({ ...storeTarget(env), ...storeCreds(env), prefixes: ['sweep/runs/'] })
  return r => lastRunOutcomeOf(r, (key, range) => store.get(key, range))
}
