import type { StagedItem } from './plans'

/** Where a map/table node stands against the open plan: `self` — staged itself (its prefix or exact
 *  object), `covered` — under a staged folder, `inside` — holds `n` staged items below it. */
export type StagedMark = { as: 'self' } | { as: 'covered'; by: string } | { as: 'inside'; n: number }

export interface StagedIndex {
  /** A node's mark: `uri` is `<scheme><path>` without a trailing `/`, `dir` whether it's a folder. */
  markOf(uri: string, dir: boolean): StagedMark | null
}

/** The open plan's items as lookups over node uris (`StagedItem.prefix`: a folder ends in `/`, an
 *  object (`kind: 'object'`) is its exact key). */
export function stagedIndex(items: readonly StagedItem[]): StagedIndex {
  const folders = new Set<string>()
  const objects = new Set<string>()
  for (const it of items) (it.kind === 'object' ? objects : folders).add(it.prefix)
  const all = [...folders, ...objects].sort()
  // Items at or below `prefix` (sorted, so a binary search finds the first, then a scan the rest).
  const countUnder = (prefix: string): number => {
    let lo = 0, hi = all.length
    while (lo < hi) { const m = (lo + hi) >> 1; if (all[m] < prefix) lo = m + 1; else hi = m }
    let n = 0
    while (lo + n < all.length && all[lo + n].startsWith(prefix)) n++
    return n
  }
  return {
    markOf(uri, dir) {
      if (dir ? folders.has(`${uri}/`) : objects.has(uri)) return { as: 'self' }
      // A staged ancestor folder: each `/` in the uri past the scheme ends one.
      const from = uri.indexOf('://') + 3
      for (let i = uri.indexOf('/', from + 1); i >= 0; i = uri.indexOf('/', i + 1)) {
        const anc = uri.slice(0, i + 1)
        if (folders.has(anc)) return { as: 'covered', by: anc }
      }
      if (!dir) return null
      const n = countUnder(`${uri}/`)
      return n ? { as: 'inside', n } : null
    },
  }
}
