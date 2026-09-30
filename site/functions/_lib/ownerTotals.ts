/** Per-user owned bytes for a scan — the ownership ledger folded server-side
 * and priced against the floor-free path index (specs/path-agnostic-serving.md
 * §2.3), so every consumer (`/users`, `/user/:id`, `/api/assignments`, the
 * user lens of `/api/subtree`) reads one number.
 *
 * Cost model: one point lookup per live claim prefix plus the depth-1 roots,
 * read once per (scan, ledger head) and cached in D1 (`owner_totals`, one
 * head at a time). A new assignment invalidates by changing the head; the
 * recompute happens on the next request. */
import type { Env } from './auth.js'
import { columnsFor, openIndex, readAsks, type Ask, type Row } from './index.js'
import { loadLedger } from './ledger.js'
import { shared } from './shared.js'
import { addAgg, computeOwners, foldLatest, idxKey, newAgg, type ClaimRow, type OwnerRow, type OwnerTotals, type PathAgg } from './claims.js'

/** The `Row` fields a total needs; `columnsFor` names them per generation. */
const FIELDS: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']
const MAX_GROUPS = 400

/** Bump when the body's shape changes: cached bodies with another version are recomputed. */
export const MANIFEST_VERSION = 1

export interface OwnerTotalsBody extends OwnerTotals {
  v: number
  scan: string
  head: number
  computed: { at: number; ms: number; groups: number; prefixes: number; index: string }
}

// Per-isolate memo on top of D1. A fresh estate manifest is a multi-second
// compute; the watchdog gives it a minute.
const memo = new Map<string, Promise<OwnerTotalsBody>>()
const MEMO_WAIT = 60_000

async function compute(env: Env, date: string, owners: Map<string, OwnerRow>, head: number): Promise<OwnerTotalsBody> {
  const t0 = Date.now()
  const idx = await openIndex(env, date)
  const want = new Map<string, number>() // index path → depth, for point lookups
  for (const p of owners.keys()) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  const asks: Ask[] = [{ depth: 1, under: '' }]
  for (const [path, depth] of want) asks.push({ depth, path })
  const isRoot = (r: Row) => r.depth === 1
  const { rows, groups } = await readAsks(
    idx,
    asks,
    r => isRoot(r) || want.get(r.path) === r.depth,
    { columns: columnsFor(idx, FIELDS), maxGroups: MAX_GROUPS },
  )
  const aggs = new Map<string, PathAgg>()
  const buckets = new Set<string>()
  for (const r of rows) {
    if (isRoot(r)) buckets.add(r.path)
    let a = aggs.get(r.path)
    if (!a) aggs.set(r.path, (a = newAgg()))
    addAgg(a, r)
  }
  const totals = computeOwners({ owners, aggs, buckets: [...buckets].sort() })
  return {
    v: MANIFEST_VERSION,
    scan: date,
    head,
    ...totals,
    computed: { at: Math.floor(Date.now() / 1000), ms: Date.now() - t0, groups, prefixes: want.size, index: idx.mode },
  }
}

/** The owner totals for `date` at the current ledger head, persisted in D1
 * (`owner_totals`, keyed by scan + head). */
export async function ownerTotals(env: Env, date: string): Promise<OwnerTotalsBody> {
  const { ownerRows, head } = await loadLedger(env)
  const key = `${date}:${head}`
  return shared(memo, key, async () => {
    const cached = await env.DB!.prepare('SELECT body FROM owner_totals WHERE scan = ? AND head = ?').bind(date, head).first<{ body: string }>()
    if (cached) {
      const body = JSON.parse(cached.body) as OwnerTotalsBody
      if (body.v === MANIFEST_VERSION) return body
    }
    const body = await compute(env, date, foldLatest(ownerRows), head)
    // Every reader keys by the live head, so bodies of older heads are dead
    // weight: drop them as this one lands.
    await env.DB!.batch([
      env.DB!.prepare('INSERT OR REPLACE INTO owner_totals (scan, head, body, claims, computed_ts, ms) VALUES (?, ?, ?, ?, ?, ?)')
        .bind(date, head, JSON.stringify(body), JSON.stringify(body.claims), body.computed.at, body.computed.ms),
      env.DB!.prepare('DELETE FROM owner_totals WHERE head < ?').bind(head),
    ])
    return body
  }, MEMO_WAIT)
}

// Claims alone, per (scan, head): a fraction of the body.
const claimsMemo = new Map<string, Promise<ClaimRow[]>>()

/** The live claims priced against `date` — what a user lens folds. A lens
 * series reads one per scan, so they are stored beside the body
 * (`owner_totals.claims`) and fetched alone. */
export async function ownerClaims(env: Env, date: string): Promise<ClaimRow[]> {
  const { head } = await loadLedger(env)
  const full = memo.get(`${date}:${head}`)
  if (full) return (await full).claims
  const key = `${date}:${head}`
  return shared(claimsMemo, key, async () => {
    const row = await env.DB!.prepare('SELECT claims FROM owner_totals WHERE scan = ? AND head = ?').bind(date, head).first<{ claims: string }>()
    if (row) return JSON.parse(row.claims) as ClaimRow[]
    return (await ownerTotals(env, date)).claims
  }, MEMO_WAIT)
}
