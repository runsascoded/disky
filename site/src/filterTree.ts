// In-memory treemap filter over the loaded (depth-capped) tree: keep the
// outermost nodes whose *segment name* matches (their whole subtree comes
// along), prune everything else, and re-aggregate ancestors from what
// survived — so sizes/attribution shown are matched bytes only, never
// double-counted. Segment-local semantics match disk-tree's `/api/filter`;
// filtering *below* the tree's depth cap is the vocab-sidecar / lakehouse
// work and arrives later.
import type { TreeNode } from './types'

export type NamePred = (name: string) => boolean
export type NodePred = (n: TreeNode) => boolean

/** `/…/` = regex (case-insensitive); anything else = substring (ci), with
 * `|` splitting alternatives (`grug|swarm` = either). Predicates receive the
 * node's full path below the root (`bucket/dir/sub`), so `grug/swarm` matches
 * across segments and regexes can span `/`. */
export function parseQuery(q: string): NamePred | null {
  const s = q.trim()
  if (!s) return null
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) {
    try {
      const re = new RegExp(s.slice(1, -1), 'i')
      return path => re.test(path)
    } catch {
      return null
    }
  }
  const needles = s.toLowerCase().split('|').map(t => t.trim()).filter(Boolean)
  if (!needles.length) return null
  return path => {
    const p = path.toLowerCase()
    return needles.some(t => p.includes(t))
  }
}

/** Re-aggregate a node's stats (b/o/tm/sh/us/d) from a filtered kid set. */
export function reaggregate(n: TreeNode, kids: TreeNode[]): TreeNode {
  const us: Record<string, number> = {}
  let b = 0
  let o = 0
  let wd = 0
  let wdb = 0
  for (const k of kids) {
    b += k.b
    o += k.o
    for (const [u, ub] of k.us ?? []) us[u] = (us[u] ?? 0) + ub
    if (k.d != null) {
      wd += k.d * k.b
      wdb += k.b
    }
  }
  const out: TreeNode = { ...n, b, o, c: kids }
  out.us = Object.keys(us).length
    ? (Object.entries(us).sort((a, c) => c[1] - a[1]) as [string, number][])
    : undefined
  out.d = wdb ? Math.round(wd / wdb) : undefined
  return out
}

/** Scope the tree to a lens's *bytes*: every node shrinks to the slice the
 * lens assigns it (a key→bytes decomposition; its sum is the node's new
 * size), descending the whole tree — unlike `applyNodeFilter`, which keeps
 * ≥minFrac subtrees whole and so lets minority co-tenant bytes ride along.
 * The result's attribution is the slice itself: no user bytes (`us` gone),
 * `tm`/`sh` are the slice (all of it userless). Objects/read-bytes scale by
 * the byte fraction (approximate). */
export function filterTree(n: TreeNode, pred: NodePred): TreeNode | null {
  if (pred(n)) return n
  const kids = (n.c ?? []).map(c => filterTree(c, pred)).filter((c): c is TreeNode => c != null)
  if (!kids.length) return null
  return reaggregate(n, kids)
}

/** Filter below the root by node predicate (root itself never matches). */
export function applyNodeFilter(root: TreeNode, pred: NodePred): TreeNode {
  const kids = (root.c ?? []).map(bucket => filterTree(bucket, pred)).filter((c): c is TreeNode => c != null)
  return reaggregate(root, kids)
}

/** The outermost matched prefixes (what a bulk action targets): every
 * non-fold node whose path matches, without descending inside matches —
 * exactly the roots `applyFilter` keeps whole. */
/** Filter below the root by *path* (fold nodes never match). Paths are the
 * node's segments below the root joined with `/` (`bucket/dir/sub`), built
 * during the walk so the predicate sees the whole ancestry. */
export function applyFilter(root: TreeNode, pred: NamePred): TreeNode {
  const walk = (n: TreeNode, path: string): TreeNode | null => {
    if (!n.n.startsWith('(') && pred(path)) return n
    const kids = (n.c ?? [])
      .map(c => walk(c, c.n.startsWith('(') ? path : `${path}/${c.n}`))
      .filter((c): c is TreeNode => c != null)
    if (!kids.length) return null
    return reaggregate(n, kids)
  }
  const kids = (root.c ?? []).map(b => walk(b, b.n)).filter((c): c is TreeNode => c != null)
  return reaggregate(root, kids)
}

/** The outermost prefixes a server-side name filter matched — nodes flagged
 * `m` by `/api/subtree?q=` (their whole subtree came along, so descendants
 * aren't flagged). Paths are segments below the root joined with `/`. */
export function collectFlagged(root: TreeNode): { path: string; b: number }[] {
  const out: { path: string; b: number }[] = []
  const walk = (n: TreeNode, path: string) => {
    if ((n as TreeNode & { m?: number }).m) {
      out.push({ path, b: n.b })
      return
    }
    for (const c of n.c ?? []) walk(c, c.n.startsWith('(') ? path : path ? `${path}/${c.n}` : c.n)
  }
  for (const b of root.c ?? []) walk(b, b.n)
  return out
}

