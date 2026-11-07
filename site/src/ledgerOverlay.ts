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
// that tile keeps its own cover until a drill draws the prefix.
//
// With `sizes` (each ledger row's subtree bytes and scan-attributed split on
// the drawn scan: the owner-totals manifest, `AssignmentRow.bytes`/`.us`),
// every node's split is exact instead — the band model of `ownerBands.ts` over
// the node's subtree, drawn or not. A depth-capped view (a share card's root,
// drawn two levels deep) has leaves far above the pixel floor, and an
// assignment inside one is a visible share of the map; without `sizes` it
// stayed the scan's. Only for a whole-subtree view: a filtered or owner-scoped
// tree's nodes hold part of their subtrees, which the manifest's totals don't.
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
  // An exact object's row touches only its strict ancestors (all its `/`-cuts).
  for (const [p, r] of [...idx.owners, ...idx.objects]) {
    if (r.owner == null) continue
    let i = p.indexOf('/', p.indexOf('://') + 3)
    while (i !== -1) {
      out.add(p.slice(0, i + 1))
      i = p.indexOf('/', i + 1)
    }
  }
  return out
}

/** A ledger row's subtree on the drawn scan: its bytes and their scan-attributed owners. */
export interface LedgerSize { b: number; us: Record<string, number> }

export function applyLedger(root: TreeNode, idx: OwnerIndex, scheme: string, canon: (u: string) => string = u => u, sizes?: ReadonlyMap<string, LedgerSize>): TreeNode {
  if (!idx.count) return root
  const touched = touchedPrefixes(idx)
  const scanVec = (us: Iterable<[string, number]>, b: number): Vec => {
    const v: Vec = new Map()
    let owned = 0
    for (const [u, x] of us) {
      add(v, canon(u), x)
      owned += x
    }
    add(v, UNOWNED, Math.max(0, b - owned))
    return v
  }
  const split = (n: TreeNode): Vec => scanVec(n.us ?? [], n.b)
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
    const who = uri ? idx.assignmentOf(uri, n.k === 'file' ? 'object' : 'prefix')?.who : segs ? null : inherited
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
  return sizes ? exact(root, idx, scheme, canon, sizes, touched, { split, scanVec, addAll, withUs }) : rec(root, [], null)
}

interface Ops {
  split: (n: TreeNode) => Vec
  scanVec: (us: Iterable<[string, number]>, b: number) => Vec
  addAll: (v: Vec, w: Vec, sign: 1 | -1) => void
  withUs: (n: TreeNode, v: Vec, cov: string | null, c?: TreeNode[]) => TreeNode
}

/** `applyLedger` with the rows' sizes: each node's split from the band model over its whole subtree.
 * S(N) = residual(N) + Σ S(C) over the outermost rows C strictly under N, where residual(N) is N's
 * cover's whole (N's bytes less the rows') or, uncovered, N's scan split less the rows'; S(C) is C's
 * band (its bytes less its trie children's, its assignee's whole, or its scan split when released)
 * plus its children's S. A fold is its parent's S less its drawn siblings', scaled to its bytes (the rest is
 * what the view drew nowhere). */
function exact(root: TreeNode, idx: OwnerIndex, scheme: string, canon: (u: string) => string, sizes: ReadonlyMap<string, LedgerSize>, touched: Set<string>, ops: Ops): TreeNode {
  const { split, scanVec, addAll, withUs } = ops
  // Every live row (releases too: a release bounds the bands around it), sorted: a folder's rows are one run.
  const keys = [...idx.owners.keys(), ...idx.objects.keys()].sort()
  const isObject = (k: string) => !k.endsWith('/')
  // The trie parent: the nearest strict ancestor folder row.
  const parent = new Map<string, string | null>()
  for (const k of keys) {
    let p: string | null = null
    const last = isObject(k) ? k.length : k.length - 1
    for (let i = k.lastIndexOf('/', last - 1); i > k.indexOf('://') + 2; i = k.lastIndexOf('/', i - 1)) {
      const a = k.slice(0, i + 1)
      if (idx.owners.has(a)) { p = a; break }
    }
    parent.set(k, p)
  }
  const kidsOf = new Map<string, string[]>()
  for (const k of keys) {
    const p = parent.get(k)
    if (p != null) kidsOf.set(p, [...(kidsOf.get(p) ?? []), k])
  }
  const lower = (key: string): number => {
    let lo = 0
    let hi = keys.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (keys[mid] < key) lo = mid + 1
      else hi = mid
    }
    return lo
  }
  /** The outermost rows strictly under folder prefix `f`. */
  const tops = (f: string): string[] => {
    const out: string[] = []
    for (let i = lower(f); i < keys.length && keys[i].startsWith(f); i++) {
      const k = keys[i]
      if (k === f) continue
      const p = parent.get(k)
      if (p == null || p.length <= f.length) out.push(k)
    }
    return out
  }
  const sizeOf = (k: string): LedgerSize => sizes.get(k) ?? { b: 0, us: {} }
  const scanOf = (k: string): Vec => { const z = sizeOf(k); return scanVec(Object.entries(z.us), z.b) }
  const assignee = (k: string): string | null => {
    const who = idx.assignmentOf(k, isObject(k) ? 'object' : 'prefix')?.who
    return who == null ? null : canon(who)
  }
  const rowMemo = new Map<string, Vec>()
  const rowVec = (k: string): Vec => {
    const hit = rowMemo.get(k)
    if (hit) return hit
    const kids = kidsOf.get(k) ?? []
    const who = assignee(k)
    const v: Vec = new Map()
    if (who != null) add(v, who, sizeOf(k).b - kids.reduce((s, d) => s + sizeOf(d).b, 0))
    else {
      addAll(v, scanOf(k), 1)
      for (const d of kids) addAll(v, scanOf(d), -1)
    }
    for (const d of kids) addAll(v, rowVec(d), 1)
    rowMemo.set(k, v)
    return v
  }
  /** A folder node's exact split (`f`: its prefix, `/`-terminated) under cover `cov`. */
  const folderVec = (n: TreeNode, f: string, cov: string | null): Vec => {
    const under = tops(f)
    const v: Vec = new Map()
    if (cov != null) add(v, cov, n.b - under.reduce((s, k) => s + sizeOf(k).b, 0))
    else {
      addAll(v, split(n), 1)
      for (const k of under) addAll(v, scanOf(k), -1)
    }
    for (const k of under) addAll(v, rowVec(k), 1)
    return v
  }
  const same = (a: Vec, b: Vec): boolean => {
    for (const k of new Set([...a.keys(), ...b.keys()])) if (Math.abs((a.get(k) ?? 0) - (b.get(k) ?? 0)) > 0.5) return false
    return true
  }
  // `uri` = the node's path (a folder's without its `/`); the root's is the scheme's prefix (`gs://`, or a drill's `gs://b/x/`).
  const rec = (n: TreeNode, uri: string, isRoot: boolean): { node: TreeNode; vec: Vec } => {
    const file = n.k === 'file'
    const f = isRoot ? uri : uri + '/'
    const cov = isRoot && !f.slice(f.indexOf('://') + 3) ? null : assignee(file ? uri : f)
    if (!isRoot && cov == null && (file || !touched.has(f))) return { node: n, vec: split(n) }
    const vec = file ? new Map([[cov!, n.b]]) : folderVec(n, f, cov)
    const kids = n.c ?? []
    if (!kids.length) return { node: same(vec, split(n)) ? n : withUs(n, vec, cov), vec }
    const drawn = kids.map(k => (k.n.startsWith('(') ? null : rec(k, f + k.n, false)))
    // The fold's share of what no drawn child holds (that, less any bytes the view drew nowhere), in its mix.
    const rest: Vec = new Map(vec)
    for (const d of drawn) if (d) addAll(rest, d.vec, -1)
    const restB = [...rest.values()].reduce((s, b) => s + Math.max(0, b), 0)
    const foldVec = (k: TreeNode): Vec => new Map([...rest].filter(([, b]) => b > 0).map(([u, b]) => [u, restB > 0 ? b * k.b / restB : 0]))
    const next = kids.map((k, i) => {
      if (drawn[i]) return drawn[i].node
      const fv = foldVec(k)
      return same(fv, split(k)) ? k : withUs(k, fv, cov)
    })
    const changed = !same(vec, split(n)) || next.some((k, i) => k !== kids[i])
    return { node: changed ? withUs(n, vec, cov, next) : n, vec }
  }
  return rec(root, scheme, true).node
}
