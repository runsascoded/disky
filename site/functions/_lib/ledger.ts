/** The live ownership ledger (assignments) and its head, straight from D1 — the
 * WAL every owner-aware read applies on top of the scan
 * (specs/view-serving.md §2). */
import type { Env } from './auth.js'
import type { OwnerRow } from './ownerBands.js'
import { shared } from './shared.js'

export interface Ledger {
  ownerRows: OwnerRow[]
  /** `MAX(actions.id)` + the tombstoned rows: changes iff the ledger changed —
   * the cache key for anything folded from it. */
  head: number
}

// The rows at a head, per isolate: the head is one cheap query every reader
// re-checks, but the rows behind it were re-read per call — a lens series
// made that read once per scan.
const rowsMemo = new Map<string, Promise<Ledger>>()

export async function loadLedger(env: Env): Promise<Ledger> {
  const head = await ledgerHead(env)
  const key = String(head)
  for (const k of rowsMemo.keys()) if (k !== key) rowsMemo.delete(k) // one head at a time
  return shared(rowsMemo, key, () => loadRows(env, head), 15_000)
}

async function loadRows(env: Env, head: number): Promise<Ledger> {
  const ownerRows = await env.DB!.prepare(
    'SELECT o.prefix, o.owner, o.ts, a.actor AS who, a.id AS action_id ' +
    'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id WHERE o.tombstoned IS NULL',
  ).all<OwnerRow>()
  return { ownerRows: ownerRows.results, head }
}

/** Does this deployment keep an ownership ledger? The gcs lineage does; cw's
 * D1 has no ledger tables and a secondary store no ledger at all, so their
 * owner pools stay the scan's attribution. Checked once per isolate and database. */
const ledgerTables = new WeakMap<object, Promise<boolean>>()
export function hasLedger(env: Env): Promise<boolean> {
  if (!env.DB || env.STORE_KEY) return Promise.resolve(false)
  let has = ledgerTables.get(env.DB)
  if (!has) {
    has = env.DB.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN ('actions', 'owner_prefixes')")
      .first<{ n: number }>().then(r => r?.n === 2)
    ledgerTables.set(env.DB, has)
  }
  return has
}

/** Just the head (one tiny query) — for cache keys before deciding whether a
 * full fold is needed. A new action raises `MAX(id)`; a retraction (an admin
 * tombstoning an `owner_prefixes` row) raises the tombstone count. Both only
 * grow, so the sum strictly increases with every change and never revisits a
 * stale key (`owner_totals` drops bodies of lower heads). */
export async function ledgerHead(env: Env): Promise<number> {
  if (!env.DB) throw new Error('ledger backend not configured (DB)')
  const row = await env.DB.prepare(
    'SELECT (SELECT COALESCE(MAX(id), 0) FROM actions) + (SELECT COUNT(*) FROM owner_prefixes WHERE tombstoned IS NOT NULL) AS head',
  ).first<{ head: number }>()
  return row?.head ?? 0
}
