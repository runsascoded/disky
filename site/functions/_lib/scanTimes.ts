/** The known starts of a store's date-only scans (`src/scanSlug.ts`
 * `ScanTimes`): each `path` pointer's `index_schema.started` (`dt-cloud
 * index-sync -m <meta.json>` records the snapshot's `meta.started`;
 * `index-started` backfills older scans), read in one D1 query — the same
 * source as the picker's `/data/<store>/scan-times.json`, so a slug resolves
 * here as it does in the page. */
import { startKey, type ScanTimes } from '../../src/scanSlug.js'
import type { Env } from './auth.js'
import { pathScanStarts } from './index.js'

/** Every date-only scan of the env's store with a known start → its start
 * instant, as recorded (ISO 8601). No D1: none. */
export async function scanStarts(env: Env): Promise<Record<string, string>> {
  const out: Record<string, string> = {}
  if (!env.DB) return out
  for (const r of (await pathScanStarts(env)).results) out[r.date] = r.started
  return out
}

/** `scans`' start keys (`startKey`): a date-only scan whose start is unknown,
 * or falls on another day, is left out (its midnight form stands). */
export async function scanTimes(env: Env, scans: readonly string[]): Promise<ScanTimes> {
  const starts = await scanStarts(env)
  const out: Record<string, string> = {}
  for (const id of scans) {
    const k = startKey(id, starts[id])
    if (k) out[id] = k
  }
  return out
}
