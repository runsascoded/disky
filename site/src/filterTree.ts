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

/** The path filter, the server's own code: the syntaxes and their registry
 * (`functions/_lib/querySyntax.ts`, text → AST) and the predicate
 * (`functions/_lib/pathQuery.ts`, AST → test on the node's full path below
 * the root, `bucket/dir/sub`). */
export { compileQuery, parseQuery } from '../functions/_lib/pathQuery'
export { DEFAULT_SYNTAX, SYNTAXES, syntaxById } from '../functions/_lib/querySyntax'

/** Re-aggregate a node's stats (bytes, objects, owners, mtime) from a filtered kid set. */
export function reaggregate(n: TreeNode, kids: TreeNode[]): TreeNode {
  const us: Record<string, number> = {}
  let bytes = 0
  let objects = 0
  let unknown = 0
  let wm = 0
  let wmb = 0
  for (const k of kids) {
    bytes += k.bytes
    objects += k.objects
    unknown += k.unknown_owner_bytes ?? 0
    for (const [u, ub] of k.owners ?? []) us[u] = (us[u] ?? 0) + ub
    if (k.mtime != null) {
      wm += k.mtime * k.bytes
      wmb += k.bytes
    }
  }
  const { unknown_owner_bytes: _unknown, ...rest } = n
  const out: TreeNode = { ...rest, bytes, objects, children: kids, ...(unknown ? { unknown_owner_bytes: unknown } : {}) }
  out.owners = Object.keys(us).length
    ? (Object.entries(us).sort((a, c) => c[1] - a[1]) as [string, number][])
    : undefined
  out.mtime = wmb ? Math.round(wm / wmb) : undefined
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
  const kids = (n.children ?? []).map(c => filterTree(c, pred)).filter((c): c is TreeNode => c != null)
  if (!kids.length) return null
  return reaggregate(n, kids)
}

/** Filter below the root by node predicate (root itself never matches). */
export function applyNodeFilter(root: TreeNode, pred: NodePred): TreeNode {
  const kids = (root.children ?? []).map(bucket => filterTree(bucket, pred)).filter((c): c is TreeNode => c != null)
  return reaggregate(root, kids)
}

/** Filter below the root by *path* (fold nodes never match). Paths are the
 * node's segments below the root joined with `/` (`bucket/dir/sub`), built
 * during the walk so the predicate sees the whole ancestry. */
export function applyFilter(root: TreeNode, pred: NamePred): TreeNode {
  const walk = (n: TreeNode, path: string): TreeNode | null => {
    if (!n.name.startsWith('(') && pred(path)) return n
    const kids = (n.children ?? [])
      .map(c => walk(c, c.name.startsWith('(') ? path : `${path}/${c.name}`))
      .filter((c): c is TreeNode => c != null)
    if (!kids.length) return null
    return reaggregate(n, kids)
  }
  const kids = (root.children ?? []).map(b => walk(b, b.name)).filter((c): c is TreeNode => c != null)
  return reaggregate(root, kids)
}
