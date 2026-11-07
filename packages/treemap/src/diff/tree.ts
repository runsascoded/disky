import type { RefObject } from 'react'
import type { DiffAreaMode, DiffInput, DiffNode } from './types'

/**
 * Recursive-diff frontier rows + the depth-1 unchanged rows (from the plain
 * compare, for labeled grey context at the top level) → a nested tree.
 *
 * Weights are bottom-up: a leaf is `max(old, new)` (or `|Δ|` in Δ mode); a
 * parent is `max(its own max, Σ children)` — churn (delete X + add Y) makes
 * children sum past either side's bytes, and the parent honestly grows to
 * hold them. Where children under-fill a parent (unchanged bytes the walk
 * never enumerated), a grey filler cell absorbs the gap, so areas stay
 * truthful without shipping every unchanged row. The server ships each
 * expanded dir's biggest unchanged children (`unchangedTop`, named grey
 * cells) and an aggregate of the rest (`unchangedRest`) — the filler's
 * tooltip counts what it stands for.
 *
 * Children order is signed: biggest adds first, unchanged middle, biggest
 * shrinks last (sort by -Δ).
 */
export function buildDiffTree(
  input: DiffInput,
  areaMode: DiffAreaMode,
  showUnchanged: boolean,
): { cells: DiffNode[]; maxAbsDelta: number } {
  const uriPrefix = input.uri.replace(/\/$/, '') + '/'
  const byPath = new Map<string, DiffNode>()
  const pathOf = new Map<DiffNode, string>()
  const roots: DiffNode[] = []
  const attach = (node: DiffNode, path: string) => {
    byPath.set(path, node)
    pathOf.set(node, path)
    const i = path.lastIndexOf('/')
    if (i < 0) {
      roots.push(node)
      return
    }
    const parentPath = path.slice(0, i)
    let parent = byPath.get(parentPath)
    if (!parent) {
      // Expanded-but-unchanged dir (e.g. a net-zero rename inside it): its
      // children were emitted without it. Synthesize the intermediate.
      parent = {
        key: uriPrefix + parentPath,
        label: parentPath.split('/').pop()!,
        weight: 0,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: 'unchanged',
        oldSize: 0,
        newSize: 0,
        countDelta: 0,
        kind: 'dir',
        uri: uriPrefix + parentPath,
        children: [],
      }
      attach(parent, parentPath)
    }
    ;(parent.children ??= []).push(node)
  }

  const recRows = [
    ...input.recRows,
    ...(showUnchanged ? input.unchangedTop : []),
  ].sort((a, b) => a.depth - b.depth || a.path.localeCompare(b.path))
  const rest = input.unchangedRest
  for (const r of recRows) {
    attach({
      key: r.uri,
      label: r.path.split('/').pop() || r.path,
      weight: 0,
      delta: r.delta,
      grew: 0,
      shrank: 0,
      nGrew: 0,
      nShrank: 0,
      status: r.status,
      oldSize: r.oldSize,
      newSize: r.newSize,
      countDelta: r.countDelta,
      kind: r.kind,
      uri: r.uri,
      pruned: r.pruned,
      children: undefined,
    }, r.path)
  }
  // Labeled grey context at the top level (the recursive walk doesn't emit
  // unchanged rows; the plain depth-1 compare does). Hidden entirely in
  // hide-unchanged mode — only the changed frontier plots.
  for (const r of showUnchanged ? input.flatRows : []) {
    if (r.status === 'unchanged' && !byPath.has(r.path)) {
      attach({
        key: r.uri,
        label: r.path,
        weight: 0,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: 'unchanged',
        oldSize: r.oldSize,
        newSize: r.newSize,
        countDelta: 0,
        kind: r.kind,
        uri: r.uri,
      }, r.path)
    }
  }

  let maxAbs = 0
  const finalize = (node: DiffNode): number => {
    maxAbs = Math.max(maxAbs, Math.abs(node.delta))
    const own = areaMode === 'max'
      ? Math.max(node.oldSize, node.newSize)
      : Math.abs(node.delta)
    if (!node.children?.length) {
      node.grew = Math.max(node.delta, 0)
      node.shrank = Math.min(node.delta, 0)
      node.nGrew = Math.max(node.countDelta, 0)
      node.nShrank = Math.min(node.countDelta, 0)
      node.weight = own
      return node.weight
    }
    let kidSum = 0
    for (const k of node.children) {
      kidSum += finalize(k)
      node.grew += k.grew
      node.shrank += k.shrank
      node.nGrew += k.nGrew
      node.nShrank += k.nShrank
    }
    // Hide-unchanged: a parent occupies only its changed children's bytes —
    // no filler for the unenumerated remainder, so areas compare *changes*,
    // not directory sizes. (Frontier leaves still carry their full
    // max(old, new) — change granularity stops there.)
    node.weight = showUnchanged ? Math.max(own, kidSum) : Math.max(kidSum, areaMode === 'max' ? 0 : own)
    const gap = node.weight - kidSum
    if (areaMode === 'max' && showUnchanged && gap > Math.max(1_000_000, node.weight * 0.002)) {
      // No name: it aggregates children the walk never enumerated — the
      // uniform grey (and the tooltip, with the server's count of what it
      // stands for) do the talking.
      const r = rest[pathOf.get(node) ?? '']
      node.children.push({
        key: `${node.key}/__unchanged__`,
        label: '',
        weight: gap,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: 'filler',
        oldSize: gap,
        newSize: gap,
        countDelta: 0,
        kind: 'filler',
        uri: node.uri,
        nRest: r?.count,
        nDescRest: r === undefined ? undefined : r.count + r.n_desc,
      })
    }
    node.children.sort((a, b) => (b.delta - a.delta) || (b.weight - a.weight))
    return node.weight
  }
  for (const r of roots) {
    finalize(r)
  }

  const cells = roots.filter(r => r.weight > 0)
  cells.sort(areaMode === 'max'
    ? (a, b) => (b.delta - a.delta) || (b.weight - a.weight)
    : (a, b) => b.weight - a.weight)
  return { cells, maxAbsDelta: maxAbs }
}

/** The smallest cell worth drawing, in px² — the widget's own fold floor. */
export const MIN_CELL_PX = 16

/**
 * Has diff detail we could fetch for it: a dir whose children the response
 * trimmed but whose own row says something changed *inside* it. Everything
 * else has no slice to fetch — an unchanged dir has no index rows at all, and
 * an added/removed one is stored as a single row by design (its whole subtree
 * is the change). Those navigate to their own page rather than drilling into
 * a blank map.
 */
export const fetchable = (n: DiffNode): boolean =>
  n.kind === 'dir'
  && !n.children?.length
  && (n.status === 'changed' || n.status === 'touched')
  && (!!n.pruned || n.delta !== 0 || n.countDelta !== 0)

/**
 * The map's drawable floor as a byte fraction: a node's screen area is ≈ its
 * share of the compared subtree × the canvas, so anything under
 * `MIN_CELL_PX / canvas_px` can't be drawn and needn't be sent.
 *
 * The first request fires before the map mounts, so fall back to the size the
 * layout will give it (page `maxWidth` 1400 minus padding; the responsive
 * height below) — an estimate beats the server's fixed default.
 */
export function mapMinFrac(ref: RefObject<HTMLDivElement | null>): number {
  const box = ref.current?.getBoundingClientRect()
  if (box && box.width > 0) return MIN_CELL_PX / (box.width * box.height)
  const vw = typeof window === 'undefined' ? 1200 : window.innerWidth
  const vh = typeof window === 'undefined' ? 800 : window.innerHeight
  const w = Math.max(320, Math.min(vw, 1400) - (vw < 600 ? 16 : 48))
  const h = vw < 600 ? Math.min(0.75 * vh, 560) : 340
  return MIN_CELL_PX / (w * h)
}
