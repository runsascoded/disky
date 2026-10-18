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
/** How a path's root reads on screen. A `file:///` store's paths are plain
 * filesystem paths (`/Applications/…`); other schemes read as their URI. */
export const pathLead = (scheme: string): string => (scheme === 'file:///' ? '/' : scheme)

export interface PathDisplay { lead: string; leadSegs: number; rest: string[] }

export function pathDisplay(scheme: string, segs: string[], home?: string[]): PathDisplay {
  if (home?.length && segs.length >= home.length && home.every((h, i) => segs[i] === h)) {
    return { lead: '~', leadSegs: home.length, rest: segs.slice(home.length) }
  }
  return { lead: pathLead(scheme), leadSegs: 0, rest: segs }
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

/** Decode browser path segments once, before expanding the home shortcut.
 * Old hand-written URLs can contain a literal `%`; keep those as literal names. */
export function fromUrlPath(path: string, home?: string[]): string[] {
  const segs = path.split('/').filter(Boolean).map(s => {
    try { return decodeURIComponent(s) } catch { return s }
  })
  return fromUrlSegs(segs, home)
}

/** Encode each actual directory name, including URL delimiters and literal `%`. */
export const toUrlPath = (segs: string[], home?: string[]): string =>
  toUrlSegs(segs, home).map(encodeURIComponent).join('/')

const leads = (segs: readonly string[], head: readonly string[]): boolean => head.every((h, i) => segs[i] === h)

/** A store path (segments from the store's data root) from a URL path's segments below the store's route: a store's
 * `root` (`Store.root`) is its mount, left out of its URLs, so it leads every path. A URL that still spells the root
 * out (a link from before the mount) reads as the same path. */
export const mountedSegs = (root: readonly string[] | undefined, urlSegs: string[]): string[] =>
  !root?.length || leads(urlSegs, root) ? urlSegs : [...root, ...urlSegs]

/** `mountedSegs`' inverse: a store path's URL segments, its root left out. */
export const urlSegsOf = (root: readonly string[] | undefined, segs: string[]): string[] =>
  root?.length && leads(segs, root) ? segs.slice(root.length) : segs

/** A store path's URL segments (below the store's route): under the store's home, `~` and the rest
 * (`toUrlSegs`); else the path with its `root` (the mount) left out (`urlSegsOf`). */
export function storeUrlSegs(store: { root?: readonly string[]; home?: string[] }, segs: string[]): string[] {
  const u = toUrlSegs(segs, store.home)
  return store.home?.length && u[0] === '~' ? u : urlSegsOf(store.root, segs)
}

/** {@link storeUrlSegs}' inverse: a URL's segments (each decoded) as a store path, `~` expanded and the mount led. */
export const storeSegsOfUrl = (store: { root?: readonly string[]; home?: string[] }, urlSegs: string[]): string[] =>
  mountedSegs(store.root, fromUrlSegs(urlSegs, store.home))
