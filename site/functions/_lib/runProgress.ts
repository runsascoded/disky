import type { D1Database } from '@cloudflare/workers-types'
import type { ProgressSample } from '../../src/runHistory.js'

/** A run's cumulative progress samples, chronological: its newest, bounded. The rate history's one source (the Runs
 * table's sparkline through `/api/plans/progress`, the run's card); rates are derived by `historyPoints`. */
export async function runSamples(db: D1Database, id: string): Promise<ProgressSample[]> {
  const { results } = await db.prepare(`
    SELECT * FROM (
      SELECT bucket, ts, deletes, bytes, gone, overwritten, failed, done, bytes_exact
      FROM deletion_progress WHERE run_id = ? ORDER BY ts DESC, bucket LIMIT 20000
    ) ORDER BY ts, bucket
  `).bind(id).all<ProgressSample>()
  return results
}
