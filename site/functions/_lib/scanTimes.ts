/** The known starts of a store's date-only scans that share a day with
 * another scan (`src/scanSlug.ts` `ScanTimes`): each read from its published
 * `meta.json`'s `started` (`path-index` writes it; `dt-cloud stamp-started`
 * back-stamps older metas). Only those scans need one, so this reads a meta
 * only for them — usually none. */
import { startKey, timesNeeded, type ScanTimes } from '../../src/scanSlug.js'
import type { Env } from './auth.js'
import { servedStore } from './index.js'
import { snapshotsPrefix } from './shared.js'

/** A scan's `meta.started`, or null (no meta, no `started`, an unreadable store). */
export async function scanStarted(env: Env, id: string): Promise<unknown> {
  try {
    const own = env.STORE_KEY ? snapshotsPrefix(env) : 'snapshots/'
    const store = servedStore(env, [own])
    const { bytes } = await store.get(`${own}${id}/meta.json`)
    const meta = JSON.parse(new TextDecoder().decode(bytes)) as { started?: unknown; captured_at?: unknown; disk_space?: { captured_at?: unknown } }
    return meta.started ?? meta.captured_at ?? meta.disk_space?.captured_at ?? null
  } catch {
    return null
  }
}

/** `scans`' needed start keys (`timesNeeded`); a scan whose start is unknown is
 * left out (its midnight form stands). */
export async function scanTimes(env: Env, scans: readonly string[], started = scanStarted): Promise<ScanTimes> {
  const need = timesNeeded(scans)
  const keys = await Promise.all(need.map(async id => startKey(id, await started(env, id))))
  const out: Record<string, string> = {}
  need.forEach((id, i) => { if (keys[i]) out[id] = keys[i]! })
  return out
}
