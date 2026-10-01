// The diff's pure model: `/api/diff` rows → the nested tree the map draws and
// the table lists. No React here, so it's unit-testable from `DiffRow` up
// (`diffRows.test.ts`); `DiffTreemap.tsx` builds it via `useDiffModel`.

const { abs, max } = Math

// `/api/diff` row (functions/_lib/view.ts `DiffRow`): p=path (relative to
// the diffed root) d=depth k=kind s=status a/b=bytes oa/ob=objects
// x=expanded l=read by a point lookup on side 1|2.
export interface DiffRow {
  p: string
  d: number
  k: 'file' | 'dir'
  s: 'added' | 'removed' | 'changed' | 'unchanged'
  a: number
  b: number
  oa: number
  ob: number
  x?: boolean
  l?: 1 | 2
}

export interface DiffData {
  prev: string | null
  curr: string | null
  total_a: number
  total_b: number
  objects_a: number
  objects_b: number
  expansions: number
  truncated: boolean
  /** The shared byte floor both scans were read at. */
  threshold: number
  lookups: number
  lookups_capped: boolean
  rows: DiffRow[]
}

export type AreaMode = 'max' | 'delta'

export interface DiffNode {
  key: string
  label: string
  /** An object or a directory (the row's `k`); a synthesized parent, the
   *  `(unchanged)` filler and the root are directories. */
  k: 'file' | 'dir'
  weight: number
  delta: number
  /** Bytes that arrived / left under this node (Σ over the frontier below
   * it): start − removed + added = end. A frontier row is all one or the other. */
  added: number
  removed: number
  status: DiffRow['s'] | 'filler' | 'root'
  size_old: number
  size_new: number
  n_desc_delta: number
  /** Object counts on each side (net only per node — the movement decomposition
   * below is the frontier sum). */
  n_old: number
  n_new: number
  /** Objects that arrived / left under this node (frontier sum, exactly like
   * `added`/`removed` for bytes): start − removed + added = end. */
  n_added: number
  n_removed: number
  lookup?: 1 | 2
  /** A root (bucket) the older scan didn't cover at all: it entered the scan,
   *  its bytes aren't the interval's writes (specs/done/root-geneses.md §3). The
   *  crumb accounts for it apart from the interval's growth, and its label
   *  says so — only the bucket carries this (see `fs` for the colour). */
  first?: boolean
  /** First-scanned for colour: the bucket AND every descendant, so a
   *  first-scanned subtree reads uniformly blue rather than green-inside-blue. */
  fs?: boolean
  children?: DiffNode[]
}

/**
 * Frontier rows → nested tree. Weights are bottom-up: a leaf is
 * `max(old, new)` (or `|Δ|` in Δ mode); a parent is `max(own, Σ children)` —
 * churn (delete X + add Y) makes children sum past either side's bytes.
 * Under-filled parents get a grey `(unchanged)` filler cell so areas stay
 * truthful without shipping every unchanged row.
 */
export function buildTree(data: DiffData, areaMode: AreaMode, atRoot: boolean): { cells: DiffNode[] } {
  const byPath = new Map<string, DiffNode>()
  const roots: DiffNode[] = []
  const attach = (node: DiffNode, path: string) => {
    byPath.set(path, node)
    const i = path.lastIndexOf('/')
    if (i < 0) {
      roots.push(node)
      return
    }
    const parentPath = path.slice(0, i)
    let parent = byPath.get(parentPath)
    if (!parent) {
      // Expanded-but-net-zero dir whose children were emitted without it.
      parent = {
        key: parentPath, label: parentPath.split('/').pop()!, k: 'dir', weight: 0, delta: 0, added: 0, removed: 0,
        status: 'unchanged', size_old: 0, size_new: 0, n_desc_delta: 0, n_old: 0, n_new: 0, n_added: 0, n_removed: 0, children: [],
      }
      attach(parent, parentPath)
    }
    ;(parent.children ??= []).push(node)
  }

  const rows = [...data.rows].sort((a, b) => a.d - b.d || a.p.localeCompare(b.p))
  for (const r of rows) {
    attach({
      key: r.p,
      label: r.p.split('/').pop() || r.p,
      k: r.k,
      weight: 0,
      delta: r.b - r.a,
      added: r.x ? 0 : max(0, r.b - r.a),
      removed: r.x ? 0 : max(0, r.a - r.b),
      status: r.s,
      size_old: r.a,
      size_new: r.b,
      n_desc_delta: r.ob - r.oa,
      n_old: r.oa,
      n_new: r.ob,
      n_added: r.x ? 0 : max(0, r.ob - r.oa),
      n_removed: r.x ? 0 : max(0, r.oa - r.ob),
      ...(atRoot && r.d === 1 && r.s === 'added' ? { first: true } : {}),
      lookup: r.l,
    }, r.p)
  }

  const finalize = (node: DiffNode): number => {
    const own = areaMode === 'max' ? max(node.size_old, node.size_new) : abs(node.delta)
    if (!node.children?.length) {
      node.weight = own
      return node.weight
    }
    let kidSum = 0
    for (const k of node.children) {
      kidSum += finalize(k)
      node.added += k.added
      node.removed += k.removed
      node.n_added += k.n_added
      node.n_removed += k.n_removed
    }
    node.weight = max(own, kidSum)
    const gap = node.weight - kidSum
    if (areaMode === 'max' && gap > max(1_000_000, node.weight * 0.002)) {
      node.children.push({
        key: `${node.key}/__unchanged__`, label: '(unchanged)', k: 'dir', weight: gap, delta: 0, added: 0, removed: 0,
        status: 'filler', size_old: gap, size_new: gap, n_desc_delta: 0, n_old: 0, n_new: 0, n_added: 0, n_removed: 0,
      })
    }
    node.children.sort((a, b) => (b.delta - a.delta) || (b.weight - a.weight))
    return node.weight
  }
  for (const r of roots) finalize(r)

  // First-scanned buckets colour their whole subtree blue (see FIRST_SCANNED):
  // mark the bucket and every descendant `fs` once the tree is built.
  const markFs = (node: DiffNode) => {
    node.fs = true
    for (const k of node.children ?? []) markFs(k)
  }
  for (const r of roots) if (r.first) markFs(r)

  const cells = roots.filter(r => r.weight > 0)
  cells.sort(areaMode === 'max'
    ? (a, b) => (b.delta - a.delta) || (b.weight - a.weight)
    : (a, b) => b.weight - a.weight)
  return { cells }
}
