/** Which card a page URL gets (specs/done/dogi.md): its kind, the canonical view
 * params the card is drawn from (and that a view token is minted over), and
 * the unfurl's title. Pure: no env, no I/O. */
import { mountedSegs, urlSegsOf } from '../../../src/pathCrumbs.js'
import { sectionParam } from '../../../src/homeSections.js'
import { canonicalSel } from '../../../src/scanSlug.js'

export type OgKind = 'map' | 'diff' | 'series' | 'staged' | 'users' | 'user' | 'assignments'

/** The home sections with a card of their own (`?v=diff`, `?v=time`), by kind: the diff, and size over time. */
export const KIND_SECTION: Partial<Record<OgKind, string>> = { diff: 'diff', series: 'over-time' }

export interface PageView {
  kind: OgKind
  /** The view: exactly what changes the card. Order-free; empties dropped
   *  when signed (`canonical`). */
  params: Record<string, string>
  title: string
}

/** First path segments that are not map pages: API and asset routes, and the
 * app's other pages (handled below or left to the static card). Every page route
 * in `src/Root.tsx` is one of these or a card kind (`routes.test.ts` checks): an
 * app page read as a folder drew an empty map of a path named `scans`, `meta`, …. */
const NOT_MAP = new Set([
  'a', 'api', 'auth', 'data', 'v1', 'og', 'slack', 'assets', '_fonts', 'files', 'admin', 'about', 'help', 'privacy', 'token', 'login',
  'signin', 'dev', 'meta', 'runs', 'scans', 'health', 'names', 'dups', 'sweep', 'hot', 'marks', 'mark',
])

/** The section a map page's view is of, in words. */
const SECTION_WORDS: Partial<Record<OgKind, string>> = { diff: 'changes', series: 'size over time' }

/** Map params a card depends on: the scan (`d`), the filter (`f`, `qs`), the
 * owner lens or pool (`o`, `by`), the colour axis (`c`) and classes (`cl`). */
const MAP_KEYS = ['d', 'f', 'qs', 'o', 'by', 'c', 'cl'] as const
/** A series card's: the map's less the colour axis (`c`), which no series follows. */
const SERIES_KEYS = MAP_KEYS.filter(k => k !== 'c')

const pick = (sp: URLSearchParams, keys: readonly string[]): Record<string, string> => {
  const out: Record<string, string> = {}
  // A bare `o` is a value (the unowned pool); other keys need one. The scan
  // (`d`) is canonicalized — legacy `?date=`/`?from=` and ISO spellings fold
  // into the page's compact `?d=` (`scanSlug.ts`), so a card keys by the
  // same view whichever spelling the shared link used.
  for (const k of keys) {
    const v = k === 'd' ? canonicalSel(sp) : sp.get(k)
    if (v || (v === '' && k === 'o')) out[k] = v
  }
  return out
}

/** A secondary store's page path (`STORES_JSON` `path`, `/`-led, no trailing `/`): its key and URL prefix. */
export interface StorePath { key: string; path: string }

/** The secondary store whose `path` leads `pathname` (the longest, as the frontend's `storeForPath`), with the
 * pathname below it (`/meta/a/b` → `/a/b`, `/meta` → `/`); null = the primary's page. */
export function storeOfPath(pathname: string, stores: readonly StorePath[]): { key: string; rest: string } | null {
  const s = [...stores].sort((a, b) => b.path.length - a.path.length).find(s => pathname === s.path || pathname.startsWith(`${s.path}/`))
  return s ? { key: s.key, rest: pathname.slice(s.path.length) || '/' } : null
}

/** First segments under a secondary store's path that are not its map: the object opener, its scan runs and the OG
 * render page (`src/Root.tsx`'s `<path>/files/*`, `<path>/scans/*`, `<path>/og`). */
const NOT_STORE_MAP = new Set(['files', 'scans', 'og'])

/** The page's view, or null for pages with no card of their own (API routes,
 * `/files`, `/admin`, unknown asset-looking paths). `site` names the root; `root` is the store's mount
 * (`Store.root`), which leads every map path and which its URLs leave out. `store`: a secondary store's key, `url`
 * then being the page below its path (`storeOfPath`'s `rest`); its pages are map and diff only, and their view
 * carries `store` (what the card reads, as the data Functions do). */
export function pageView(url: URL, site: string, root: readonly string[] = [], store?: string): PageView | null {
  const segs = url.pathname.split('/').filter(Boolean).map(s => { try { return decodeURIComponent(s) } catch { return s } })
  const sp = url.searchParams
  const head = segs[0] ?? ''
  if (store !== undefined) {
    if (NOT_STORE_MAP.has(head) || segs.some(s => s.includes('..'))) return null
    return mapView(segs, sp, site, root, { store })
  }
  if (head === 'staged' && segs.length === 1) {
    const params = pick(sp, ['q'])
    return { kind: 'staged', params, title: params.q ? `Staged for deletion: “${params.q}”` : 'Staged for deletion' }
  }
  if (head === 'users' && segs.length === 1) return { kind: 'users', params: pick(sp, ['d']), title: `${site} — users` }
  if (head === 'user' && segs.length === 2) return { kind: 'user', params: { id: segs[1], ...pick(sp, ['d']) }, title: `${segs[1]} · ${site}` }
  if (head === 'assignments' && segs.length === 1) return { kind: 'assignments', params: pick(sp, ['d']), title: `${site} — assigner × assignee` }
  // their other shapes are the OG render pages (`/users/og`, `/user/:id/og`) and a redirect (`/users/*` → `/`)
  if (head === 'users' || head === 'user' || head === 'assignments' || head === 'staged') return null
  if (NOT_MAP.has(head) || segs.some(s => s.includes('..'))) return null
  // A file-looking single segment at the root (`/og.jpg`, `/favicon.svg`) is an asset.
  if (segs.length === 1 && /\.(?:png|jpe?g|svg|ico|txt|json|js|css|webmanifest|wasm|ttf)$/i.test(head)) return null
  return mapView(segs, sp, site, root, {})
}

/** A map page's view (`segs` below the store's path), its diff section's and its size-over-time section's; `lead`
 * leads the params. */
function mapView(segs: string[], sp: URLSearchParams, site: string, root: readonly string[], lead: Record<string, string>): PageView {
  const tree = mountedSegs(root, segs)
  const path = tree.join('/')
  const section = sectionParam(sp)
  const params: Record<string, string> = { ...lead, ...(path ? { path } : {}), ...pick(sp, section === KIND_SECTION.series ? SERIES_KEYS : MAP_KEYS) }
  const where = urlSegsOf(root, tree).join('/') || site
  const extras = [params.f ? `filter: ${params.f}` : null, params.o !== undefined ? `owner: ${params.o === '' ? 'unowned' : params.o === '*' ? 'owned' : params.o}` : null].filter(Boolean)
  // The section in view (`?v=`, or a legacy `?section=`: the home page's scroll-spy writes it, share links carry it).
  if (section === KIND_SECTION.diff) {
    const dm = sp.get('dm')
    return { kind: 'diff', params: { ...params, ...(dm === 'max' ? { dm } : {}) }, title: [where, SECTION_WORDS.diff, ...extras].join(' · ') }
  }
  if (section === KIND_SECTION.series) return { kind: 'series', params, title: [where, SECTION_WORDS.series, ...extras].join(' · ') }
  return { kind: 'map', params, title: [where, ...extras].join(' · ') }
}

/** What an anonymous unfurl calls a page when its URL names it: a map page's path below the mount (and its section), a
 * user's id. Null: nothing in the URL, so the page's name in `policy.ts`, or the site alone at a map's root. Never the
 * URL's filter or lens (`f`, `o`), as `/staged` never echoes its `q`. */
export function urlName(pv: PageView, root: readonly string[] = []): string | null {
  if (pv.kind === 'user') return pv.params.id
  if (pv.kind !== 'map' && pv.kind !== 'diff' && pv.kind !== 'series') return null
  const where = urlSegsOf(root, pv.params.path?.split('/') ?? []).join('/')
  return [where, SECTION_WORDS[pv.kind]].filter(Boolean).join(' · ') || null
}

/** The primary's pages with no card kind and one path shape, by first segment: the `policy.ts` page each is, or leads
 * to (`src/Root.tsx`'s retired routes: `/hot`, `/sweep`, `/mark`, `/marks`). */
const SHELL_PAGES: Record<string, string> = {
  dups: 'dups', names: 'names', health: 'health', privacy: 'privacy', signin: 'signin',
  hot: 'names', sweep: 'staged', mark: 'map', marks: 'map',
}

/** The named page a pathname with no `pageView` is, or null (API routes, assets, `/runs`' own Function, a path no route
 * of `src/Root.tsx` names). `inStore`: a pathname below a secondary store's path, whose other pages are its map.
 * `id`: `/scans/<id>`'s, decoded and unvalidated. */
export function shellPage(pathname: string, inStore = false): { page: string; id?: string } | null {
  const segs = pathname.split('/').filter(Boolean)
  const head = segs[0] ?? ''
  if (head === 'scans') {
    if (segs.length === 1) return { page: 'scans' }
    try {
      return { page: 'scan', id: decodeURIComponent(segs.slice(1).join('/')) }
    } catch {
      return { page: 'scan' }
    }
  }
  if (head === 'files') return { page: 'files' }
  if (inStore) return null
  if (head === 'admin') return { page: 'admin' }
  return segs.length === 1 && Object.hasOwn(SHELL_PAGES, head) ? { page: SHELL_PAGES[head] } : null
}
