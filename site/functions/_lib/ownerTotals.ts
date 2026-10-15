/** Per-user owned bytes for a scan — the ownership ledger folded server-side
 * and priced against the floor-free path index (specs/done/path-agnostic-serving.md
 * §2.3), so every consumer (`/users`, `/user/:id`, `/api/assignments`, the
 * user lens of `/api/subtree`) reads one number.
 *
 * Cost model: one point lookup per live assignment prefix plus the depth-1 roots.
 * A prefix's bytes/objects on a scan never change (the scan's index is
 * immutable), so they are kept per (scan, index) in D1 (`owner_prefix_aggs`)
 * and only prefixes not priced yet are read — a new assignment's on every
 * interval-store scan at once. Folding the ledger over a scan's cached prefixes
 * is cheap (~0.1 s), so a folded body is only memoized in the isolate: an owner
 * change writes nothing per scan. (Folded bodies stored per (scan, ledger head)
 * cost ~290 KB × every scan on each change; re-pricing every live prefix on a
 * new head, ~22 s a scan on gcs, made an owner-lens series time out.) */
import type { Env } from './auth.js'
import { columnsFor, ivHeldDates, openIndex, slices, readAsks, type Ask, type Row } from './index.js'
import { fillHist, histAggs } from './ownerHist.js'
import { loadLedger } from './ledger.js'
import { joinOf, shared } from './shared.js'
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
  computed: { at: number; ms: number; groups: number; prefixes: number; index: string; hist?: true }
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

async function storePriced(env: Env, date: string, tag: string, cache: PrefixCache): Promise<void> {
  const body = JSON.stringify(cache)
  if (body.length > PREFIX_CACHE_MAX) return
  await env.DB!.prepare('INSERT OR REPLACE INTO owner_prefix_aggs (scan, idx, body, updated_ts) VALUES (?, ?, ?, ?)')
    .bind(date, tag, body, Math.floor(Date.now() / 1000)).run()
}

/** The ledger's prefixes as index lookups: path → depth. */
function wantOf(owners: Map<string, OwnerRow>): Map<string, number> {
  const want = new Map<string, number>()
  for (const p of owners.keys()) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  return want
}

async function compute(env: Env, date: string, owners: Map<string, OwnerRow>, head: number): Promise<OwnerTotalsBody> {
  const t0 = Date.now()
  const idx = await openIndex(slices(env), date)
  const tag = indexTag(idx)
  const want = wantOf(owners)
  // An interval-store scan folds from the prefixes' history rows (one per prefix, every scan); the rest — other
  // stores, scans appended since the rows were priced, or a first fill still pending — per scan below.
  const hist = idx.asOf != null ? await histAggs(slices(env), tag, idx.asOf, want) : null
  if (hist) {
    const totals = computeOwners({ owners, aggs: hist.aggs, buckets: hist.buckets })
    return {
      v: MANIFEST_VERSION, scan: date, head, ...totals,
      computed: { at: Math.floor(Date.now() / 1000), ms: Date.now() - t0, groups: hist.groups, prefixes: hist.prefixes, index: idx.mode, hist: true },
    }
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
    cache.asked = [...new Set([...cache.asked, ...[...missing].map(([path, depth]) => `${depth}:${path}`)])].sort()
    await storePriced(env, date, tag, cache)
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

/** The owner totals for `date` at the current ledger head: folded from the scan's cached prefixes (a refold
 * after an owner change reads and writes nothing per scan), memoized per (scan, head) in the isolate. */
export async function ownerTotals(env: Env, date: string): Promise<OwnerTotalsBody> {
  const { ownerRows, head } = await loadLedger(env)
  return shared(memo, `${date}:${head}`, () => compute(env, date, foldLatest(ownerRows), head), MEMO_WAIT, joinOf(env, 'owner-totals'))
}

/** The live assignments priced against `date` — what a user lens folds. */
export async function ownerAssignments(env: Env, date: string): Promise<AssignmentRow[]> {
  return (await ownerTotals(env, date)).assignments
}

/** Fill the history rows for the ledger's prefixes not yet priced through the interval store's newest scan,
 * `n` per call (one read each): the first fill, and catching up after scans are appended. */
export async function fillOwnerHist(env: Env, n: number): Promise<{ priced: number; remaining: number; groups: number; index: string } | null> {
  const dates = await ivHeldDates(slices(env))
  if (!dates.length) return null
  const idx = await openIndex(slices(env), dates[dates.length - 1])
  const tag = indexTag(idx)
  const { ownerRows } = await loadLedger(env)
  const got = await fillHist(slices(env), tag, wantOf(foldLatest(ownerRows)), n)
  return got && { ...got, index: tag }
}
