/** Which card a page URL gets (specs/done/dogi.md): its kind, the canonical view
 * params the card is drawn from (and that a view token is minted over), and
 * the unfurl's title. Pure: no env, no I/O. */
import { mountedSegs, urlSegsOf } from '../../../src/pathCrumbs.js'
import { canonicalSel } from '../../../src/scanSlug.js'

export type OgKind = 'map' | 'diff' | 'staged' | 'users' | 'user' | 'assignments'

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
  'api', 'auth', 'data', 'v1', 'og', 'slack', 'assets', '_fonts', 'files', 'admin', 'about', 'help', 'privacy', 'token', 'login',
  'signin', 'dev', 'meta', 'runs', 'scans', 'health', 'names', 'sweep', 'hot', 'marks', 'mark',
])

/** Map params a card depends on: the scan (`d`), the filter (`f`, `qs`), the
 * owner lens or pool (`o`, `by`), the colour axis (`c`) and classes (`cl`). */
const MAP_KEYS = ['d', 'f', 'qs', 'o', 'by', 'c', 'cl'] as const

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

/** A map page's view (`segs` below the store's path) and its diff section's; `lead` leads the params. */
function mapView(segs: string[], sp: URLSearchParams, site: string, root: readonly string[], lead: Record<string, string>): PageView {
  const tree = mountedSegs(root, segs)
  const path = tree.join('/')
  const params: Record<string, string> = { ...lead, ...(path ? { path } : {}), ...pick(sp, MAP_KEYS) }
  const where = urlSegsOf(root, tree).join('/') || site
  const extras = [params.f ? `filter: ${params.f}` : null, params.o !== undefined ? `owner: ${params.o === '' ? 'unowned' : params.o === '*' ? 'owned' : params.o}` : null].filter(Boolean)
  if (sp.get('section') === 'diff') {
    const dm = sp.get('dm')
    return { kind: 'diff', params: { ...params, ...(dm === 'max' ? { dm } : {}) }, title: [where, 'changes', ...extras].join(' · ') }
  }
  return { kind: 'map', params, title: [where, ...extras].join(' · ') }
}
