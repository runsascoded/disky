// The diff map's cell colours and root, DOM-free: the in-app map
// (`DiffTreemap.tsx`) and the server-drawn digest image (`functions/_lib/og/
// digestCard.ts`) paint a change the same way.
import { divergingColor, divergingInk } from '@open-athena/treemap/divergingColor'
import { buildTree, type AreaMode, type DiffData, type DiffNode } from './diffModel'

const { abs, max, min, sign } = Math

// `divergingColor` is red-positive, so negate on the way in.
export const UNCHANGED_GREY = 'rgba(110, 118, 129, 0.28)'
export const deltaColor = (t: number) => divergingColor(-t)
// A root the older scan never covered (specs/done/root-geneses.md §3): neither
// grew nor shrank, so neither green nor red — the site's blue. A first-scanned
// bucket AND everything under it read blue: its bytes entered the scan, they
// aren't the interval's writes, so painting its interior green (as "added")
// would double-count the day's growth. Blue frames a blue interior — never a
// blue box sitting under green children.
export const FIRST_SCANNED = 'var(--s1)'

export interface DiffCellStyle { bg: string; ink: string; edge?: string }

/** One cell's fill and ink. */
export function diffCellStyle(n: DiffNode, areaMode: AreaMode): DiffCellStyle {
  // First-scanned: the bucket and its whole subtree read blue. A container
  // gets a neutral fill with a blue frame (the borders draw it), so its blue
  // children aren't sitting inside a blue block; a leaf takes the blue fill.
  if (n.fs) {
    return n.children?.length
      ? { bg: 'color-mix(in oklab, var(--s1) 28%, var(--panel))', ink: 'var(--ink)', edge: FIRST_SCANNED }
      : { bg: FIRST_SCANNED, ink: '#fff', edge: FIRST_SCANNED }
  }
  // No `edge` below: the treemap's adaptive contrast half-stroke (dark on
  // light fills, light on dark) draws the seams, as on the main map — a
  // fill-tinted edge left green-on-green cells without a visible border.
  if (areaMode === 'max') {
    if (n.children?.length) {
      const t = n.weight === 0 ? 0 : n.delta / n.weight
      return { bg: deltaColor(t), ink: divergingInk(t) }
    }
    const f = n.weight === 0 ? 0 : min(1, abs(n.delta) / n.weight)
    if (f === 0) return { bg: UNCHANGED_GREY, ink: divergingInk(0) }
    const pct = `${(f * 100).toFixed(2)}%`
    const band = deltaColor(sign(n.delta))
    return {
      bg: `linear-gradient(to top, ${band} ${pct}, ${UNCHANGED_GREY} ${pct})`,
      ink: divergingInk(f > 0.85 ? 1 : 0),
    }
  }
  // Δ mode: area already says how much moved; color says how much of the
  // node that was — a wholly added / removed directory is full green / red
  // however small, a 5% shrink is a faint red, a net-zero churn is grey (its
  // tooltip shows the churn).
  const base = max(n.size_old, n.size_new)
  const t = base === 0 ? 0 : n.delta / base
  return { bg: deltaColor(t), ink: divergingInk(t) }
}

/** The diff's tree under one synthetic root labelled `label`, in `mode`
 *  (falling back to `max` when Δ has nothing to show and `mode` wasn't
 *  pinned), or null when nothing moved. */
export function diffRoot(data: DiffData, mode: AreaMode, pinned: boolean, atRoot: boolean, label: string): { root: DiffNode; mode: AreaMode } | null {
  let { cells } = buildTree(data, mode, atRoot)
  if (!pinned && mode === 'delta' && cells.length === 0) {
    const alt = buildTree(data, 'max', atRoot)
    if (alt.cells.length) { mode = 'max'; cells = alt.cells }
  }
  if (!cells.length) return null
  const root: DiffNode = {
    key: label,
    label,
    k: 'dir',
    weight: cells.reduce((s, c) => s + c.weight, 0),
    delta: data.total_b - data.total_a,
    added: cells.reduce((s, c) => s + c.added, 0),
    removed: cells.reduce((s, c) => s + c.removed, 0),
    status: 'root',
    size_old: data.total_a,
    size_new: data.total_b,
    n_desc_delta: data.objects_b - data.objects_a,
    n_old: data.objects_a,
    n_new: data.objects_b,
    n_added: cells.reduce((s, c) => s + c.n_added, 0),
    n_removed: cells.reduce((s, c) => s + c.n_removed, 0),
    children: cells,
  }
  return { root, mode }
}

/** The borders: a fat gutter at the top level, a clear line one down,
 *  hairlines below — so nested cells read as nested, not one flat mosaic. */
export function diffBorderWidth(depth: number, { w, h }: { w: number; h: number }): number {
  const base = depth === 0 ? 3 : depth === 1 ? 2 : 1
  return min(base, max(1, min(w, h) / 16))
}
