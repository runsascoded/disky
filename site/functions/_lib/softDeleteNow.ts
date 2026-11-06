// What the buckets' soft delete is now, as far as the site can know: the job's newest reading
// (`snapshots/soft-delete/<date>.json`, the scan step `soft-delete`, `dt-cloud soft-delete -o`), read through the served
// seam. The site's own GCP account submits Batch jobs and nothing else (no `storage.buckets.get`), so it can't ask GCS; the
// job's account can, and writes what it reads after each scan. So a reading is as old as the last scan that ran the step
// (each use says when), a bucket it doesn't name is unknown, and the undo job asks GCS itself (`sweep_exec.undo_run`).
// One read behind the pages (`/api/sweep/soft-delete`), the undo route's gate (`sweepUndo.ts`) and the Slack thread
// (`stagedSlack.ts`). The cost snapshot (`/api/cost/gcp`) is no source for this: it covers the deployment's own buckets,
// not the ones a sweep deletes from.
import type { D1Database } from '@cloudflare/workers-types'
import type { Store } from '@rdub/file-tree'
import type { Env } from './auth.js'
import { servedStore, storeReady } from './index.js'
import { type BucketSoftDelete, bucketSoftDeleteOf, runUndoUnavailable, runUndoUnseen, type RunUndoWindow, type SoftDeleteSnapshot, type UndoUnavailable, type UndoUnseen } from '../../src/softDelete.js'

export const SOFT_DELETE_PREFIX = 'snapshots/soft-delete/'
const DATE_RE = /^(\d{4}-\d{2}-\d{2})\.json$/

/** The newest day's document under the prefix (null: none written). */
export async function newestSoftDelete(store: Pick<Store, 'list' | 'get'>): Promise<SoftDeleteSnapshot | null> {
  let newest: string | null = null
  let cursor: string | undefined
  do {
    const page = await store.list(SOFT_DELETE_PREFIX, { cursor })
    for (const e of page.entries) if (!e.isDir && DATE_RE.test(e.key.slice(SOFT_DELETE_PREFIX.length)) && (newest === null || e.key > newest)) newest = e.key
    cursor = page.cursor
  } while (cursor)
  return newest ? JSON.parse(new TextDecoder().decode((await store.get(newest)).bytes)) as SoftDeleteSnapshot : null
}

/** The newest reading (null: none: the step isn't run here, a store not configured, or a read that failed, which is logged).
 * Never throws: with no reading the site vouches for nothing, and says so. */
export async function bucketSoftDelete(env: Env): Promise<BucketSoftDelete | null> {
  if (env.SERVED_R2 ? !env.INDEX_R2 : !storeReady(env)) return null
  try {
    return bucketSoftDeleteOf(await newestSoftDelete(servedStore(env, [SOFT_DELETE_PREFIX])))
  } catch (error) {
    console.log(`bucket soft delete: ${(error as Error).message}`)
    return null
  }
}

/** The `deletion_runs` columns that say which buckets a run deleted from. */
export interface RunBucketsRow { run_id: string; buckets?: string | null }

/** The buckets a run touched: its recorded `-b` cut, else the buckets its
 * bands name (`gs://<bucket>/…`), sorted. */
export async function runBuckets(db: D1Database, row: RunBucketsRow): Promise<string[]> {
  if (row.buckets) return row.buckets.split(',').map(b => b.trim()).filter(Boolean).sort()
  const bands = (await db.prepare('SELECT prefix FROM deletion_bands WHERE run_id = ?').bind(row.run_id).all<{ prefix: string }>()).results
  return [...new Set(bands.map(b => /^[a-z0-9]+:\/\/([^/]+)/.exec(b.prefix)?.[1]).filter((b): b is string => !!b))].sort()
}

/** What the reading says of the undo `row` recorded, at `now`: `unavailable` (soft delete is off on a bucket it deleted from),
 * `unseen` (nothing says, for some bucket of it), or neither (every bucket of it is on, as of the reading). Reads nothing
 * for a run with no open window, or one already undone: it has no undo to qualify. */
export async function runUndoNow(
  env: Env,
  db: D1Database,
  row: RunBucketsRow & RunUndoWindow,
  now: number,
): Promise<{ unavailable: UndoUnavailable | null; unseen: UndoUnseen | null }> {
  if (row.mode !== 'real' || row.undo_deadline == null || now >= row.undo_deadline || row.undo_state === 'full') return { unavailable: null, unseen: null }
  const [known, buckets] = await Promise.all([bucketSoftDelete(env), runBuckets(db, row)])
  return { unavailable: runUndoUnavailable(row, buckets, known, now), unseen: runUndoUnseen(row, buckets, known, now) }
}
