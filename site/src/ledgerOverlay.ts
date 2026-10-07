// The ownership ledger applied to the map (specs/done/path-agnostic-serving.md
// §2.3, "Owner coloring: applied client-side"): `/api/subtree` serves each
// node's owner split (`us`) as the scan attributed it — the committed
// state — and this overlays the live assignments (the WAL) on the drawn
// tree, so cells, the rollup legend, tooltips and the table's owner bars say
// what the table's owner column (`OwnerIndex.assignmentOf`) says, the moment an
// assignment lands.
//
// Per drawn node: a node covered by an assignment (the newest live row on its
// ancestor-or-self chain, `assignmentOf`) is the assignee's whole when it's a leaf
// tile; a branch's split is its scan split plus its children's changes, plus
// — under an assignment — the bytes no drawn child accounts for. A fold
// (`(other)`) has no path: it takes its parent's assignee. What the overlay
// can't see is an assigned prefix strictly inside an undrawn tile or a fold —
// that tile keeps its own cover until a drill draws the prefix (it is below
// the view's pixel floor, so its color share is too).
import type { OwnerIndex } from './ownerIndex'
import type { TreeNode } from './types'

/** Owner → bytes, with the unowned remainder under a key no user id can be. */
type Vec = Map<string, number>
const UNOWNED = '\0'

const add = (v: Vec, k: string, b: number) => v.set(k, (v.get(k) ?? 0) + b)

/** Prefix of every assigned prefix, and the prefix itself ('gs://b/x/' →
 * 'gs://b/', 'gs://b/x/'): the subtrees the overlay must walk into. */
function touchedPrefixes(idx: OwnerIndex): Set<string> {
  const out = new Set<string>()
  for (const [p, r] of idx.owners) {
    if (r.owner == null) continue
    let i = p.indexOf('/', p.indexOf('://') + 3)
    while (i !== -1) {
      out.add(p.slice(0, i + 1))
      i = p.indexOf('/', i + 1)
    }
  }
  return out
}

export function applyLedger(root: TreeNode, idx: OwnerIndex, scheme: string, canon: (u: string) => string = u => u): TreeNode {
  if (!idx.count) return root
  const touched = touchedPrefixes(idx)
  const split = (n: TreeNode): Vec => {
    const v: Vec = new Map()
    let owned = 0
    for (const [u, b] of n.us ?? []) {
      add(v, canon(u), b)
      owned += b
    }
    add(v, UNOWNED, Math.max(0, n.b - owned))
    return v
  }
  const addAll = (v: Vec, w: Vec, sign: 1 | -1) => { for (const [k, b] of w) add(v, k, sign * b) }
  const top = (us: TreeNode['us']) => (us?.length ? canon(us[0][0]) : null)
  const withUs = (n: TreeNode, v: Vec, cov: string | null, c?: TreeNode[]): TreeNode => {
    const us = [...v.entries()]
      .filter(([k, b]) => k !== UNOWNED && b > 0.5)
      .sort((x, y) => y[1] - x[1] || (x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : 0))
    const { us: _us, pv, c: _c, ...rest } = n
    // Provenance explains the scan's top owner: gone once an assignment
    // covers the node or moves its top owner.
    const keepPv = pv && cov == null && top(us) === top(n.us)
    return { ...rest, ...(us.length ? { us } : {}), ...(keepPv ? { pv } : {}), ...(c ? { c } : {}) }
  }
  // `segs` = the node's path below the root; null = a fold (no path of its
  // own), which inherits `inherited` — its parent's assignee.
  const rec = (n: TreeNode, segs: string[] | null, inherited: string | null): TreeNode => {
    const isRoot = segs?.length === 0
    const uri = segs && !isRoot ? scheme + segs.join('/') : null
    const who = uri ? idx.assignmentOf(uri)?.who : segs ? null : inherited
    const cov = who == null ? null : canon(who)
    if (cov == null && !isRoot && (!uri || !touched.has(uri + '/'))) return n
    const kids = n.c ?? []
    if (!kids.length) return cov == null ? n : withUs(n, new Map([[cov, n.b]]), cov)
    const next = kids.map(k => rec(k, segs && !k.n.startsWith('(') ? [...segs, k.n] : null, cov))
    const v = split(n)
    let changed = false
    next.forEach((k, i) => {
      if (k === kids[i]) return
      changed = true
      addAll(v, split(k), 1)
      addAll(v, split(kids[i]), -1)
    })
    // Bytes no drawn child accounts for are the assignee's too.
    const rest = n.b - kids.reduce((s, k) => s + k.b, 0)
    if (cov != null && rest > 0.5) {
      const res = split(n)
      for (const k of kids) addAll(res, split(k), -1)
      for (const [k, b] of res) if (b > 0) add(v, k, -b)
      add(v, cov, rest)
      changed = true
    }
    return changed ? withUs(n, v, cov, next) : n
  }
  return rec(root, [], null)
}
