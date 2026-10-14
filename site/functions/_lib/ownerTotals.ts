/** Per-user owned bytes for a scan — the ownership ledger folded server-side
 * and priced against the floor-free path index (specs/done/path-agnostic-serving.md
 * §2.3), so every consumer (`/users`, `/user/:id`, `/api/assignments`, the
 * user lens of `/api/subtree`) reads one number.
 *
 * Cost model: one point lookup per live assignment prefix plus the depth-1 roots.
 * A prefix's bytes/objects on a scan never change (the scan's index is
 * immutable), so they are kept per (scan, index) in D1 (`owner_prefix_aggs`)
 * and only prefixes not priced yet are read. The folded body is cached per
 * (scan, ledger head) (`owner_totals`, one head at a time): a new assignment
 * changes the head, and the refold re-reads just its new prefixes — not every
 * live one (~1,260 on gcs, ~22 s a scan), which made an owner-lens series
 * (one fold per scan) time out after any assignment. */
import type { Env } from './auth.js'
import { columnsFor, openIndex, slices, readAsks, type Ask, type Row } from './index.js'
import { loadLedger } from './ledger.js'
import { shared } from './shared.js'
import { addAgg, computeOwners, foldLatest, idxKey, newAgg, type AssignmentRow, type OwnerRow, type OwnerTotals, type PathAgg } from './ownerBands.js'

/** The `Row` fields a total needs; `columnsFor` names them per generation. */
const FIELDS: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']
/** Row groups a totals read may decode: each point lookup needs at most two
 * (its row on a group boundary), so the bound scales with the assignment count —
 * gcs's ~1,260 prefixes on 8k-row store groups need more than a flat 400. */
const maxGroups = (asks: number) => Math.max(400, 2 * asks)

/** Bump when the body's shape changes: cached bodies with another version are recomputed. */
export const MANIFEST_VERSION = 3  // 2: `claims` → `assignments`; 3: per-user objects (`uo`)

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

/** A scan's priced prefixes (`owner_prefix_aggs.body`): every `depth:path` asked so far (absent from `aggs` =
 *  not in the scan), the depth-1 roots, and each present path's bytes / objects / owners / classes. */
interface PrefixCache { v: number; buckets: string[]; asked: string[]; aggs: Record<string, PathAgg> }
/** Bump when `PathAgg`'s shape changes: other versions are re-priced. */
const PREFIX_CACHE_VERSION = 1
/** A cached body larger than this isn't stored (D1's row limit is 2 MB); the next fold prices it again. */
const PREFIX_CACHE_MAX = 1_800_000

/** The index a scan's prefixes are priced on: a reindex (new generation) prices them again. */
const indexTag = (idx: { mode: string }): string => `${idx.mode}:${'gen' in idx ? (idx as { gen: string }).gen : ''}`

async function compute(env: Env, date: string, owners: Map<string, OwnerRow>, head: number): Promise<OwnerTotalsBody> {
  const t0 = Date.now()
  const idx = await openIndex(slices(env), date)
  const tag = indexTag(idx)
  const want = new Map<string, number>() // index path → depth, for point lookups
  for (const p of owners.keys()) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  const row = await env.DB!.prepare('SELECT body FROM owner_prefix_aggs WHERE scan = ? AND idx = ?').bind(date, tag).first<{ body: string }>()
  const prior = row ? JSON.parse(row.body) as PrefixCache : null
  const cache: PrefixCache = prior?.v === PREFIX_CACHE_VERSION ? prior : { v: PREFIX_CACHE_VERSION, buckets: [], asked: [], aggs: {} }
  const asked = new Set(cache.asked)
  const fresh = cache !== prior
  const asks: Ask[] = fresh ? [{ depth: 1, under: '' }] : []
  const missing = new Map<string, number>()
  for (const [path, depth] of want) if (!asked.has(`${depth}:${path}`)) { missing.set(path, depth); asks.push({ depth, path }) }
  let groups = 0
  if (asks.length) {
    const isRoot = (r: Row) => fresh && r.depth === 1
    const read = await readAsks(
      idx,
      asks,
      r => isRoot(r) || missing.get(r.path) === r.depth,
      { columns: columnsFor(idx, FIELDS), maxGroups: maxGroups(asks.length) },
    )
    groups = read.groups
    const added = new Map<string, PathAgg>()
    const buckets = new Set(cache.buckets)
    for (const r of read.rows) {
      if (isRoot(r)) buckets.add(r.path)
      // A path priced before (a root that is also an assigned prefix, say) keeps its one aggregate.
      if (cache.aggs[r.path] && !added.has(r.path)) continue
      let a = added.get(r.path)
      if (!a) added.set(r.path, (a = newAgg()))
      addAgg(a, r)
    }
    for (const [path, a] of added) cache.aggs[path] = a
    cache.buckets = [...buckets].sort()
    for (const [path, depth] of missing) asked.add(`${depth}:${path}`)
    cache.asked = [...asked].sort()
    const body = JSON.stringify(cache)
    if (body.length <= PREFIX_CACHE_MAX) {
      await env.DB!.prepare('INSERT OR REPLACE INTO owner_prefix_aggs (scan, idx, body, updated_ts) VALUES (?, ?, ?, ?)')
        .bind(date, tag, body, Math.floor(Date.now() / 1000)).run()
    }
  }
  const aggs = new Map(Object.entries(cache.aggs))
  const totals = computeOwners({ owners, aggs, buckets: cache.buckets })
  return {
    v: MANIFEST_VERSION,
    scan: date,
    head,
    ...totals,
    computed: { at: Math.floor(Date.now() / 1000), ms: Date.now() - t0, groups, prefixes: missing.size, index: idx.mode },
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
        .bind(date, head, JSON.stringify(body), JSON.stringify(body.assignments), body.computed.at, body.computed.ms),
      env.DB!.prepare('DELETE FROM owner_totals WHERE head < ?').bind(head),
    ])
    return body
  }, MEMO_WAIT)
}

// Assignments alone, per (scan, head): a fraction of the body.
const assignmentsMemo = new Map<string, Promise<AssignmentRow[]>>()

/** The live assignments priced against `date` — what a user lens folds. A lens
 * series reads one per scan, so they are stored beside the body
 * (`owner_totals.claims`) and fetched alone. */
export async function ownerAssignments(env: Env, date: string): Promise<AssignmentRow[]> {
  const { head } = await loadLedger(env)
  const full = memo.get(`${date}:${head}`)
  if (full) return (await full).assignments
  const key = `${date}:${head}`
  return shared(assignmentsMemo, key, async () => {
    const row = await env.DB!.prepare('SELECT claims FROM owner_totals WHERE scan = ? AND head = ?').bind(date, head).first<{ claims: string }>()
    // Rows stored before `uo` (manifest v2) recompute with the body.
    const rows = row ? JSON.parse(row.claims) as AssignmentRow[] : null
    if (rows && rows.every(r => r.uo)) return rows
    return (await ownerTotals(env, date)).assignments
  }, MEMO_WAIT)
}
