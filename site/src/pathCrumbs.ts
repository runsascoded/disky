// Breadcrumbs for a drilled path — the pure half of the docked tip's `PathBar`
// (Treemap.tsx) and the page bar's crumbs (App.tsx). Paths are segment names
// under a store's root; the store's `scheme` (`gs://`, `s3://`, `r2://`)
// prefixes the URI a copy button hands to the CLI.

export interface Crumb {
  name: string
  /** The segments from the root through this one — what drilling here selects. */
  segs: string[]
  /** Ancestors drill; the deepest (the node itself) and a folded `(other)` are inert. */
  drillable: boolean
  last: boolean
}

/** A treemap's `(other)` / `(files)` folds: synthetic children, not prefixes. */
export const isFold = (name: string): boolean => name.startsWith('(')

/** One crumb per segment of a path (the root excluded). */
export function pathCrumbs(names: string[]): Crumb[] {
  return names.map((name, i) => {
    const last = i === names.length - 1
    return { name, segs: names.slice(0, i + 1), drillable: !last && !isFold(name), last }
  })
}

/** `<scheme><segs joined by '/'>` — with a trailing `/` when `dir` and the path
 * isn't the root, so a prefix pastes into `ls`-style tools as a directory. */
export function pathUri(scheme: string, segs: string[], dir = false): string {
  return scheme + segs.join('/') + (dir && segs.length ? '/' : '')
}
