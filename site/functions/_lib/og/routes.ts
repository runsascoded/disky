/** Which card a page URL gets (specs/done/dogi.md): its kind, the canonical view
 * params the card is drawn from (and that a view token is minted over), and
 * the unfurl's title. Pure: no env, no I/O. */

export type OgKind = 'map' | 'staged' | 'users' | 'user' | 'assignments'

export interface PageView {
  kind: OgKind
  /** The view: exactly what changes the card. Order-free; empties dropped
   *  when signed (`canonical`). */
  params: Record<string, string>
  title: string
}

/** First path segments that are not map pages: API and asset routes, and the
 * app's other pages (handled below or left to the static card). */
const NOT_MAP = new Set(['api', 'auth', 'data', 'v1', 'og', 'slack', 'assets', '_fonts', 'files', 'admin', 'about', 'help', 'privacy', 'token', 'login'])

/** Map params a card depends on: the scan (`d`), the filter (`f`, `qs`), the
 * owner lens or pool (`o`, `by`), the colour axis (`c`) and classes (`cl`). */
const MAP_KEYS = ['d', 'f', 'qs', 'o', 'by', 'c', 'cl'] as const

const pick = (sp: URLSearchParams, keys: readonly string[]): Record<string, string> => {
  const out: Record<string, string> = {}
  for (const k of keys) { const v = sp.get(k); if (v) out[k] = v }
  return out
}

/** The page's view, or null for pages with no card of their own (API routes,
 * `/files`, `/admin`, unknown asset-looking paths). `site` names the root. */
export function pageView(url: URL, site: string): PageView | null {
  const segs = url.pathname.split('/').filter(Boolean).map(s => { try { return decodeURIComponent(s) } catch { return s } })
  const sp = url.searchParams
  const head = segs[0] ?? ''
  if (head === 'staged' && segs.length === 1) {
    const params = pick(sp, ['q'])
    return { kind: 'staged', params, title: params.q ? `Staged for deletion: “${params.q}”` : 'Staged for deletion' }
  }
  if (head === 'users' && segs.length === 1) return { kind: 'users', params: pick(sp, ['d']), title: `${site} — users` }
  if (head === 'user' && segs.length === 2) return { kind: 'user', params: { id: segs[1], ...pick(sp, ['d']) }, title: `${segs[1]} · ${site}` }
  if (head === 'assignments' && segs.length === 1) return { kind: 'assignments', params: pick(sp, ['d']), title: `${site} — assigner × assignee` }
  if (NOT_MAP.has(head) || segs.some(s => s.includes('..'))) return null
  // A file-looking single segment at the root (`/og.jpg`, `/favicon.svg`) is an asset.
  if (segs.length === 1 && /\.(?:png|jpe?g|svg|ico|txt|json|js|css|webmanifest|wasm|ttf)$/i.test(head)) return null
  const path = segs.join('/')
  const params: Record<string, string> = { ...(path ? { path } : {}), ...pick(sp, MAP_KEYS) }
  const where = path || site
  const extras = [params.f ? `filter: ${params.f}` : null, params.o ? `owner: ${params.o}` : null].filter(Boolean)
  return { kind: 'map', params, title: [where, ...extras].join(' · ') }
}
