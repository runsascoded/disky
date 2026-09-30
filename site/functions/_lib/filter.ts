/**
 * The filter view's pure parts (specs/filter-views.md §2): which paths are
 * the match roots, how the pixel budget re-bases onto them, and what one
 * region read per root looks like. `view.ts` does the reads; this is the
 * arithmetic, spec-tested.
 */
import type { Rect } from './index.js'
import type { NamePred } from './scope.js'

export const parentOf = (p: string): string => {
  const cut = p.lastIndexOf('/')
  return cut === -1 ? '' : p.slice(0, cut)
}

/** Outermost matches among `paths`, all strictly under `root`: a path whose
 * every strict ancestor between it and the root fails the predicate. The
 * root itself matching means the whole view is matched. Sorted. */
export function matchRoots(paths: Iterable<string>, pred: NamePred, root: string): string[] {
  if (pred(root)) return [root]
  const out: string[] = []
  const under = root === '' ? () => true : (p: string) => p.startsWith(root + '/')
  for (const p of paths) {
    if (p === root || !under(p) || !pred(p)) continue
    let inner = false
    for (let a = parentOf(p); a.length > root.length; a = parentOf(a)) {
      if (pred(a)) { inner = true; break }
      if (a === '') break
    }
    if (!inner) out.push(p)
  }
  return out.sort()
}

/** One byte threshold for the whole forest: the budget split across the
 * match roots by bytes gives every root the same bytes-per-pixel, so one
 * number — the matched total's pixel threshold — serves all of them. */
export const filterThreshold = (matchedBytes: number, w: number, h: number, minArea: number): number =>
  (matchedBytes * minArea) / (w * h)

/** The per-depth threshold under one root: attenuates from the root's own
 * depth, the way the plain view attenuates from the drilled path. */
export const rebasedThreshold = (thr: number, atten: number, rootDepth: number) =>
  (depth: number): number => thr * atten ** Math.max(0, depth - rootDepth - 1)

/** The most permissive of several roots' per-depth thresholds — what one
 * multi-rect read must use (rows are then re-tested per root). */
export const looseThreshold = (thr: number, atten: number, rootDepths: number[]) => {
  const deepest = rootDepths.length ? Math.max(...rootDepths) : 0
  return rebasedThreshold(thr, atten, deepest)
}

/** Each root's subtree as a (depth, path-range) rectangle: everything below
 * it. '0' sorts just past '/', so `[path/, path0)` is exactly the subtree. */
export const rootRects = (roots: { path: string; depth: number }[]): Rect[] =>
  roots.map(r => ({ dLo: r.depth + 1, dHi: 1e9, pLo: r.path + '/', pHi: r.path + '0' }))

/** The coarsest tier whose floor the threshold can't see below, else the
 * floor-free tier — the plain planner's rule, applied to the forest. */
export function pickTier<T extends { name: string; floor: number }>(tiers: T[], thr: number): T | 'fine' {
  for (const t of [...tiers].sort((a, b) => b.floor - a.floor)) if (thr >= t.floor) return t
  return 'fine'
}

/** `?paths=a,b` or repeated `?paths=` — the series endpoint's matched set;
 * trailing slashes dropped, blanks and duplicates removed, order kept. */
export function parsePaths(values: string[]): string[] {
  const out: string[] = []
  for (const v of values) for (const p of v.split(',')) {
    const q = p.trim().replace(/\/+$/, '')
    if (q && !out.includes(q)) out.push(q)
  }
  return out
}
