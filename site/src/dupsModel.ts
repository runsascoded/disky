// /dups (specs/dups.md §3): what the page derives from `/api/dups` — each copy's idle time, whether it may be staged
// from here and why not, the suggested selection (every copy but the group's keep), the plan items a selection
// stages, and the groups' client-side sorts.
import type { PlanItem } from '../functions/_lib/plans'
import type { DupCopy, DupGroup, Tier } from '../functions/_lib/dups'

export type { DupCopy, DupGroup, Tier } from '../functions/_lib/dups'

export interface TierTotal { tier: Tier; groups: number; copies: number; bytes: number; reclaimable: number }
export interface PairTotal { tier: Tier; b1: string; b2: string; groups: number; bytes: number }
/** `dups.json`, as the `dups` step writes it (`dt-cloud dups`). */
export interface DupsSummary {
  v: number
  scan?: string
  floor: number
  suspect_copies: number
  checked_groups: number
  groups: number
  rows: number
  net_by_tier: TierTotal[]
  suspect: TierTotal[]
  by_pair: PairTotal[]
}
export interface DupsResponse {
  date: string
  summary: DupsSummary
  groups: DupGroup[]
  more: boolean
}

export type DupsSort = 'reclaim' | 'size' | 'idle'
export const SORTS: readonly DupsSort[] = ['reclaim', 'size', 'idle']

const DAY_S = 86_400
/** Today, in epoch days (`last_read`'s unit). */
export const today = (now = Date.now()): number => Math.floor(now / 1000 / DAY_S)

/** Days since the copy was last touched: its last read, else its oldest object's creation (null: neither known). */
export function idleDays(c: Pick<DupCopy, 'last_read' | 'created'>, day: number): number | null {
  if (c.last_read != null) return Math.max(0, day - c.last_read)
  if (c.created != null) return Math.max(0, day - Math.floor(c.created / DAY_S))
  return null
}

/** A group's idle time: its most recently touched copy's. */
export function groupIdle(g: DupGroup, day: number): number | null {
  const ds = g.items.map(c => idleDays(c, day)).filter((d): d is number => d != null)
  return ds.length ? Math.min(...ds) : null
}

/** Why a copy can't be staged from /dups, or null. A `moved` copy needs a passing spot check (an `equal` verdict
 *  against the keep copy): same size and basename alone are often a collision. A copy whose objects differ from the
 *  keep copy's isn't a duplicate. */
export function stageBlock(g: DupGroup, c: DupCopy): string | null {
  if (c.staged != null) return `already staged (plan ${c.staged})`
  if (c.check === 'differs') return "its objects differ from the keep copy's (spot check)"
  if (g.tier === 'moved' && c.check !== 'equal') return c.keep ? 'the suggested keep' : 'a moved copy needs a passing spot check first'
  return null
}

/** The copies pre-checked for staging: every stageable copy but the keep, and none nested under another group's copy
 *  (its bytes go with that copy). */
export const suggested = (g: DupGroup): string[] =>
  g.items.filter(c => !c.keep && !c.nested && stageBlock(g, c) == null).map(c => c.path)

/** What staging copies adds to the plan: a dir as its prefix (`<scheme><path>/`), an object exactly. */
export const stageItems = (copies: { path: string; kind: 'dir' | 'file' }[], scheme: string): PlanItem[] =>
  copies.map(c => (c.kind === 'dir' ? { key: `${scheme}${c.path}/`, kind: 'prefix' } : { key: `${scheme}${c.path}`, kind: 'object' }))

/** The groups in `sort` order: reclaimable bytes (the report's own), copy size, or idle time (longest first;
 *  unknown last). Ties keep the report's order. */
export function sortGroups(gs: readonly DupGroup[], sort: DupsSort, day: number): DupGroup[] {
  if (sort === 'reclaim') return [...gs]
  const key = (g: DupGroup) => (sort === 'size' ? g.size : groupIdle(g, day) ?? -1)
  return gs.map((g, i) => [g, i] as const).sort((a, b) => key(b[0]) - key(a[0]) || a[1] - b[1]).map(([g]) => g)
}

/** The bucket-pair matrix of one tier: its buckets (sorted) and each pair's bytes (`b1 ≤ b2`; a diagonal cell is a
 *  bucket holding ≥2 copies). */
export function pairMatrix(pairs: readonly PairTotal[], tier: Tier): { buckets: string[]; cell: (a: string, b: string) => PairTotal | undefined } {
  const mine = pairs.filter(p => p.tier === tier)
  const buckets = [...new Set(mine.flatMap(p => [p.b1, p.b2]))].sort()
  const at = new Map(mine.map(p => [`${p.b1}|${p.b2}`, p]))
  return { buckets, cell: (a, b) => at.get(a <= b ? `${a}|${b}` : `${b}|${a}`) }
}
