// A real run's totals when it has a start row and no end record: its job was killed, or its final `record_run` failed. Read from
// what the executor left in its run dir (specs/done/sweep-killed-run-totals.md):
//
//   1. `deleted-summary.json`: the executor writes it once every bucket's counts are final, so its totals are exact.
//   2. else each bucket's `progress/<bucket>.json`, rewritten every 30 s: acknowledged deletes up to its last write, so a lower bound.
//
// Not the `deleted/` journal parts: a Worker would have to decode a parquet column over hundreds of files (65,536 rows each) to
// sum decisions and bytes (a footer's row count mixes decisions and has no byte sum), and they lag too, up to a part's rows in the
// local spool. A progress file is one small JSON per bucket. Reads are one at a time, each cut at `PROGRESS_BYTES`, the whole
// time-boxed: at most one file's bytes in memory, once per run (the row closes), and a bad store costs `READ_MS`, not the route.
import { NotFoundError, type GetResult } from '@rdub/file-tree'
import { S3Store } from '@rdub/file-tree/stores/s3'
import { storeCreds, storeReady, storeTarget } from './index.js'

/** A cut file doesn't parse: a summary then falls to progress, a progress file to unread. */
export const PROGRESS_BYTES = 4 * 1024 * 1024
export const READ_MS = 8000

type Get = (key: string, range: { offset: number; length: number }) => Promise<Pick<GetResult, 'bytes'>>

export interface RunTotals {
  deleted_objects: number
  deleted_bytes: number
  skipped_gone: number
  skipped_overwritten: number
  drift_dirs: number
  /** From the final summary; else a lower bound. */
  exact: boolean
}

interface BucketCounts { decisions?: Record<string, number>; delete_bytes?: number; drift_dirs?: unknown[] }

const sum = (buckets: readonly BucketCounts[], exact: boolean): RunTotals => ({
  deleted_objects: buckets.reduce((n, b) => n + (b.decisions?.delete ?? 0), 0),
  deleted_bytes: buckets.reduce((n, b) => n + (b.delete_bytes ?? 0), 0),
  skipped_gone: buckets.reduce((n, b) => n + (b.decisions?.skipped_gone ?? 0), 0),
  skipped_overwritten: buckets.reduce((n, b) => n + (b.decisions?.skipped_overwritten ?? 0), 0),
  drift_dirs: buckets.reduce((n, b) => n + (b.drift_dirs?.length ?? 0), 0),
  exact,
})

/** The file as JSON; null: it isn't there. Throws on a failed read or a document that doesn't parse. */
async function readJson<T>(key: string, get: Get): Promise<T | null> {
  try {
    const { bytes } = await get(key, { offset: 0, length: PROGRESS_BYTES })
    return JSON.parse(new TextDecoder().decode(bytes)) as T
  } catch (error) {
    if (error instanceof NotFoundError) return null
    throw error
  }
}

/** `buckets`: the row's `buckets` column (null: `plan-summary.json`'s, for an image from before every run recorded them). */
export async function runTotalsOf(jobId: string, buckets: string | null, get: Get): Promise<RunTotals> {
  const dir = `sweep/runs/${jobId}`
  const summary = await readJson<{ buckets?: Record<string, BucketCounts> }>(`${dir}/deleted-summary.json`, get).catch(error => {
    if (!(error instanceof SyntaxError)) throw error
    console.log(`totals of ${jobId}: deleted-summary.json over ${PROGRESS_BYTES} bytes`)
    return null
  })
  if (summary?.buckets) return sum(Object.values(summary.buckets), true)
  const names = buckets?.split(',').map(b => b.trim()).filter(Boolean)
    ?? Object.keys((await readJson<{ buckets?: Record<string, unknown> }>(`${dir}/plan-summary.json`, get))?.buckets ?? {})
  const progress: BucketCounts[] = []
  for (const bucket of names) {
    const file = await readJson<BucketCounts>(`${dir}/progress/${bucket}.json`, get)
    if (file) progress.push({ decisions: file.decisions, delete_bytes: file.delete_bytes })
  }
  return sum(progress, false)
}

/** Null: unread, for this poll. */
export type TotalsReader = (jobId: string, buckets: string | null) => Promise<RunTotals | null>

/** Never throws, and answers within `READ_MS`. */
export const totalsReaderOf = (get: Get): TotalsReader => async (jobId, buckets) => {
  let timer: ReturnType<typeof setTimeout> | null = null
  const late = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(`read took over ${READ_MS} ms`)), READ_MS) })
  try {
    return await Promise.race([runTotalsOf(jobId, buckets, get), late])
  } catch (error) {
    console.log(`totals of ${jobId}: ${(error as Error).message}`)
    return null
  } finally {
    clearTimeout(timer)
  }
}

/** A reader over the deployment's store. */
export function totalsReader(env: Parameters<typeof storeReady>[0]): TotalsReader | undefined {
  if (!storeReady(env)) return undefined
  const store = S3Store({ ...storeTarget(env), ...storeCreds(env), prefixes: ['sweep/runs/'] })
  return totalsReaderOf((key, range) => store.get(key, range))
}
