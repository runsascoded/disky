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

/** A treemap's `(other)` fold: synthetic children, not a prefix. */
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

/** How a path's root reads on screen. A `file:///` store's paths are plain
 * filesystem paths (`/Applications/…`); other schemes read as their URI. */
export const pathLead = (scheme: string): string => (scheme === 'file:///' ? '/' : scheme)

/** A path as it reads on screen: `/Applications`, `gs://b/x` — a trailing `/`
 * when `dir` and the path isn't the root. */
export function pathText(scheme: string, segs: string[], dir = false): string {
  return pathLead(scheme) + segs.join('/') + (dir && segs.length ? '/' : '')
}

/** What a copy button hands the clipboard: a `file:///` store's absolute
 * filesystem path (shells and `disk-tree` take it as is), else the URI. */
export function pathCopy(scheme: string, segs: string[], dir = false): string {
  return scheme === 'file:///' ? '/' + segs.join('/') + (dir && segs.length ? '/' : '') : pathUri(scheme, segs, dir)
}

/** A browser path's segments, each decoded once (`toUrlPath`'s inverse). Old
 * hand-written URLs can carry a literal `%`; those stay literal names. */
export function fromUrlPath(path: string): string[] {
  return path.split('/').filter(Boolean).map(s => {
    try { return decodeURIComponent(s) } catch { return s }
  })
}

/** Each actual dir name encoded, URL delimiters and literal `%` included. */
export const toUrlPath = (segs: string[]): string => segs.map(encodeURIComponent).join('/')

const leads = (segs: readonly string[], head: readonly string[]): boolean => head.every((h, i) => segs[i] === h)

/** A store path (segments from the store's data root) from a URL path's segments below the store's route: a store's
 * `root` (`Store.root`) is its mount, left out of its URLs, so it leads every path. A URL that still spells the root
 * out (a link from before the mount) reads as the same path. */
export const mountedSegs = (root: readonly string[] | undefined, urlSegs: string[]): string[] =>
  !root?.length || leads(urlSegs, root) ? urlSegs : [...root, ...urlSegs]

/** `mountedSegs`' inverse: a store path's URL segments, its root left out. */
export const urlSegsOf = (root: readonly string[] | undefined, segs: string[]): string[] =>
  root?.length && leads(segs, root) ? segs.slice(root.length) : segs
