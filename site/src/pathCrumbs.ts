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

/** How a path reads on screen. A `file:///` store's paths are plain
 * filesystem paths (`/Applications/…`); a store `home` (its segments, e.g.
 * `['Users', 'ryan']`) folds to `~`. Other schemes read as their URI. `lead`
 * stands for the first `leadSegs` segments; `rest` follows it. */
export interface PathDisplay { lead: string; leadSegs: number; rest: string[] }

export function pathDisplay(scheme: string, segs: string[], home?: string[]): PathDisplay {
  if (home?.length && segs.length >= home.length && home.every((h, i) => segs[i] === h)) {
    return { lead: '~', leadSegs: home.length, rest: segs.slice(home.length) }
  }
  return { lead: scheme === 'file:///' ? '/' : scheme, leadSegs: 0, rest: segs }
}

/** {@link pathDisplay} as one string: `~/c/disky`, `/Applications`, `gs://b/x`
 * — a trailing `/` when `dir` and the path isn't its lead alone. */
export function pathText(scheme: string, segs: string[], home?: string[], dir = false): string {
  const { lead, rest } = pathDisplay(scheme, segs, home)
  const body = rest.join('/') + (dir && rest.length ? '/' : '')
  return lead === '~' ? (body ? `~/${body}` : '~') : lead + body
}

/** What a copy button hands the clipboard: a `file:///` store's absolute
 * filesystem path (shells and `disk-tree` take it as is), else the URI. */
export function pathCopy(scheme: string, segs: string[], dir = false): string {
  return scheme === 'file:///' ? '/' + segs.join('/') + (dir && segs.length ? '/' : '') : pathUri(scheme, segs, dir)
}

/** A drill path's URL form: a store home folds to a leading `~` segment
 * (`/~/c/disky`), so home URLs are short and read like the crumbs. */
export function toUrlSegs(segs: string[], home?: string[]): string[] {
  const { lead, leadSegs, rest } = pathDisplay('', segs, home)
  return lead === '~' ? ['~', ...rest] : segs.slice(leadSegs)
}

/** The inverse of {@link toUrlSegs}: a leading `~` expands to the home. */
export function fromUrlSegs(urlSegs: string[], home?: string[]): string[] {
  return home?.length && urlSegs[0] === '~' ? [...home, ...urlSegs.slice(1)] : urlSegs
}
