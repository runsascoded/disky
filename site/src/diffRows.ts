// The diff table's row model: the diff map's cells (one drilled node's
// children, as `buildTree` laid them out — named dirs, the `(other)` fold and
// any `(unchanged)` filler) → one flat row each, with the before/after/Δ
// numbers the map's tooltip shows, plus the column sorts. Pure, so the
// derivation and every sort order are unit-tested (`diffRows.test.ts`).
import type { DiffNode } from './diffModel'

const { abs } = Math

export type DiffStatus = 'added' | 'removed' | 'changed' | 'unchanged' | 'first'

export interface DiffTableRow {
  key: string
  name: string
  /** Path segments below the diffed node — the drill target for a named
   *  directory; empty for a fold / filler, which never drills. */
  segs: string[]
  status: DiffStatus
  /** A synthetic row: the `(other)` fold or the `(unchanged)` filler. */
  synthetic: boolean
  a: number
  b: number
  delta: number
  /** Δ as a share of the before bytes; `null` when there were none. */
  pct: number | null
  oa: number
  ob: number
  odelta: number
}

/** Δ / before, or `null` when nothing was there before (an added row's
 *  growth is unbounded, not 100%). */
export const deltaPct = (a: number, b: number): number | null => (a > 0 ? (b - a) / a : null)

/** A row's status: the diff's own, except that a first-scanned root (entered
 *  the scan this interval — specs/root-geneses.md §3) reads as `first`, not
 *  `added`, and the filler cell is `unchanged`. */
export const statusOf = (n: DiffNode): DiffStatus =>
  n.first ? 'first' : n.status === 'filler' || n.status === 'root' ? 'unchanged' : n.status

export const isSynthetic = (n: DiffNode): boolean => n.status === 'filler' || n.label.startsWith('(')

export function diffTableRows(cells: DiffNode[]): DiffTableRow[] {
  return cells.map(n => {
    const synthetic = isSynthetic(n)
    return {
      key: n.key,
      name: n.label,
      segs: synthetic ? [] : n.key.split('/'),
      status: statusOf(n),
      synthetic,
      a: n.size_old,
      b: n.size_new,
      delta: n.delta,
      pct: deltaPct(n.size_old, n.size_new),
      oa: n.n_old,
      ob: n.n_new,
      odelta: n.n_desc_delta,
    }
  })
}

export type SortKey = 'name' | 'status' | 'a' | 'b' | 'delta' | 'pct' | 'oa' | 'ob' | 'odelta'

/** The direction a column starts in when first clicked: names A→Z, statuses
 *  in movement order; every number biggest-first. */
export const defaultAsc = (k: SortKey): boolean => k === 'name' || k === 'status'

const STATUS_ORDER: Record<DiffStatus, number> = { added: 0, first: 1, removed: 2, changed: 3, unchanged: 4 }

/** Sort key per column. Δ columns sort by magnitude (a −5 Ti shrink ranks
 *  beside a +5 Ti growth, not below every tiny gain); a missing `pct` sorts
 *  last either way. */
const sortVal = (r: DiffTableRow, k: SortKey): number | string =>
  k === 'name' ? r.name
  : k === 'status' ? STATUS_ORDER[r.status]
  : k === 'delta' ? abs(r.delta)
  : k === 'odelta' ? abs(r.odelta)
  : k === 'pct' ? (r.pct == null ? NaN : abs(r.pct))
  : r[k]

/** Stable: rows that tie keep their incoming order (the map's cell order). */
export function sortDiffRows(rows: DiffTableRow[], k: SortKey, asc: boolean): DiffTableRow[] {
  const dir = asc ? 1 : -1
  return rows.slice().sort((x, y) => {
    const vx = sortVal(x, k)
    const vy = sortVal(y, k)
    if (typeof vx === 'string' || typeof vy === 'string') return String(vx).localeCompare(String(vy)) * dir
    const nx = Number.isNaN(vx)
    const ny = Number.isNaN(vy)
    if (nx || ny) return nx === ny ? 0 : nx ? 1 : -1
    return (vx - vy) * dir
  })
}

/** `+12.5%`, `−100%`, `+3.2k%` — a signed percentage at a precision that
 *  suits its size. */
export function fmtPct(p: number): string {
  const s = p >= 0 ? '+' : '−'
  const v = abs(p) * 100
  const body = v >= 1000 ? `${(v / 1000).toPrecision(2)}k` : v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)
  return `${s}${body}%`
}
