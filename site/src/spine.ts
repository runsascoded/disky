import type { TreeNode } from './types'

// The drilled map's spine: the levels from the store root to the drilled path, what each asks of `/api/subtree`,
// and how their answers graft into the one tree the map draws. A level has two reads: its *full* view (the
// pixel-budget tree, every depth) and its *shallow* one (`depth=1`: the same top-level children, one band).

/** The drilled path's levels, root first: `''`, then each prefix of `graftPath`. */
export function spinePaths(graftPath: string): string[] {
  const segs = graftPath.split('/').filter(Boolean)
  return ['', ...segs.map((_, i) => segs.slice(0, i + 1).join('/'))]
}

/** Which of its two reads level `i` of `n` sends. */
export interface LevelAsk { full: boolean; shallow: boolean }

/**
 * The drilled view draws only the deepest level's subtree: that level asks both reads (the shallow one paints
 * first). An ancestor is drawn when the viewer zooms out to it, and until then gives the spine its node and its
 * children's totals, which its shallow read holds: it asks that alone, and its full view when it becomes the
 * deepest. A full answer already held (`fullHeld`: the viewer drilled down through it) is used as it is, with no
 * shallow read beside it.
 * Under a name filter every level asks both, as before: a level's forest of match roots is not a prefix of the
 * one below it.
 */
export function levelAsk(i: number, n: number, o: { filtered: boolean; fullHeld: boolean }): LevelAsk {
  const deepest = i === n - 1
  if (o.filtered) return { full: true, shallow: true }
  return { full: deepest, shallow: deepest || !o.fullHeld }
}

/**
 * A level's tree for the graft: its full view once here, else its shallow one. Under a filter an ancestor's
 * first-paint forest stands in only when it carries the next spine segment (`next`) with children (an ancestor of
 * match roots); else that level waits for its full forest.
 */
export function levelTree(o: { full?: TreeNode | null; shallow?: TreeNode | null; deepest: boolean; filtered: boolean; next?: string }): TreeNode | null {
  if (o.full) return o.full
  const s = o.shallow ?? null
  if (o.deepest || !o.filtered) return s
  return s && s.c?.some(k => k.n === o.next && k.c?.length) ? s : null
}

/** One level's answer, as the graft takes it: `shallow` when it is the `depth=1` stand-in. */
export interface Level { tree: TreeNode; shallow: boolean }

/**
 * `sub` (the answer for the path `segs`) grafted into `t`: the node there keeps its own totals and adopts the
 * answer's children.
 */
function graftAt(t: TreeNode, segs: string[], sub: TreeNode, shallow: boolean): TreeNode {
  const rec = (n: TreeNode, i: number): TreeNode => {
    // A depth-1 stand-in never replaces deeper children the parent's
    // tree already carries for this node: a drill into a tile the parent
    // drew to depth would otherwise flatten it to one level until the
    // full subtree lands (seconds, on a cold path).
    if (i === segs.length && shallow && n.c?.some(k => k.c?.length)) return n
    // Keep own totals; adopt the finer children — and the response root's
    // provenance (`pv`), which a parent-level view may have skipped.
    if (i === segs.length) return { ...n, c: sub.c, ...(sub.pv ? { pv: sub.pv } : {}) }
    const seg = segs[i]
    const kids = n.c ?? []
    if (kids.some(k => k.n === seg)) return { ...n, c: kids.map(k => (k.n === seg ? rec(k, i + 1) : k)) }
    // The spine segment fell below this level's pixel budget (it's inside
    // "(other)"): synthesize it from its own subtree response — the
    // response root carries the real totals — and shave those bytes off
    // the fold so the level still sums. Deeper segments wait for their
    // own level's graft to land.
    if (i !== segs.length - 1) return n
    const c = kids.map(k =>
      k.n === '(other)' ? { ...k, b: Math.max(0, k.b - sub.b), o: Math.max(0, k.o - sub.o) } : k)
    return { ...n, c: [...c, { ...sub, n: seg }] }
  }
  return rec(t, 0)
}

/** The map's tree: the root level's answer with each deeper level's grafted in, in depth order (`levels[i]` answers
 *  `paths[i]`; null: not here). Null until the root level answers. */
export function graftSpine(paths: string[], levels: (Level | null)[]): TreeNode | null {
  let t = levels[0]?.tree ?? null
  if (!t) return null
  paths.forEach((p, i) => {
    const l = levels[i]
    if (p && l) t = graftAt(t!, p.split('/'), l.tree, l.shallow)
  })
  return t
}

/** The node at `segs` under `tree`; null when the walk stops short of it. */
export function nodeAt(tree: TreeNode, segs: string[]): TreeNode | null {
  let cur = tree
  for (const s of segs) {
    const next = cur.c?.find(c => c.n === s)
    if (!next) return null
    cur = next
  }
  return cur
}

/**
 * Whether the grafted `tree` can draw the drilled path, given which levels are still loading with no answer
 * (`loading[i]`): not while the spine stops short of the path and a level that could extend it is loading, and not
 * while the drilled node has no children and its own level is loading. The page holds what it drew until then,
 * never an ancestor's view or an empty canvas in passing. Settled levels (answered or failed) are drawn as they
 * are: a path the scan doesn't hold truncates to its deepest ancestor.
 */
export function spineReady(tree: TreeNode, graftPath: string, loading: boolean[]): boolean {
  const at = nodeAt(tree, graftPath.split('/').filter(Boolean))
  if (!at) return !loading.some(Boolean)
  return !!at.c?.length || !loading[loading.length - 1]
}
