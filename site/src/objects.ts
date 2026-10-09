// Objects as first-class nodes (specs/path-store.md §3): what a click on a
// cell or a row does, where an opened object's bytes come from, and where an
// old `/files/…` link lands. Pure, so every rule is specced in
// `objects.test.ts`; the components (`Treemap`, `ChildrenTable`,
// `DiffTreemap`, `DiffTable`, `ObjectPanel`, `FilesRedirect`) only call them.
import { isFold } from './pathCrumbs'
import type { Store } from './stores'

/** The sorts a store generation's view is answered from (`View.tier`,
 * functions/_lib/view.ts): `bysize` / `path`, or under a lens `bysize-user` /
 * `user`. A v1 (dir-only) generation answers from `coarse<E>` / `fine`; a
 * filter view names both phases (`<first>+<second>`), so the last one decides. */
const STORE_TIERS = new Set(['bysize', 'path', 'bysize-user', 'user'])

/** Whether a view's scan lists objects — a store generation (`index_schema`
 * version 2), whose leaves can be objects — rather than a v1 index, whose
 * every leaf is a directory. Read off the response's `tier`. */
export const listsObjects = (tier: string | undefined): boolean =>
  !!tier && STORE_TIERS.has(tier.split('+').pop()!)

/** The fields a click decision reads off a treemap node. */
export interface CellNode {
  n: string
  k?: 'file' | 'dir'
  o: number
  c?: unknown[]
}

/** What a click on a cell does:
 * - `open`: an object — the leaf viewer opens it (`?open=`).
 * - `drill`: a directory — the page drills there (its own fetch brings its
 *   children, so a childless cell still drills).
 * - `pin`: the core's default — the cell's tip pins. A fold (`(other)`), an
 *   ⌥-click (pin any cell without leaving the view), and, on a v1 scan, a
 *   childless directory holding at most one object: a v1 index never lists
 *   objects, so drilling there would draw nothing. */
export type CellAction = 'open' | 'drill' | 'pin'

export function cellAction(n: CellNode, objects: boolean, alt = false): CellAction {
  if (alt || isFold(n.n)) return 'pin'
  if (n.k === 'file') return 'open'
  if (!objects && !n.c?.length && n.o <= 1) return 'pin'
  return 'drill'
}

/** A children-table / diff-table row's link: an object opens in the leaf
 * viewer (`name` under the drilled directory), a directory drills (`segs`
 * from the store root); a fold or filler row is not a link. */
export type RowTarget =
  | { kind: 'open'; segs: string[] }
  | { kind: 'drill'; segs: string[] }
  | null

export function rowTarget(segs: string[], name: string, k: 'file' | 'dir' | undefined, synthetic = isFold(name)): RowTarget {
  if (synthetic) return null
  return { kind: k === 'file' ? 'open' : 'drill', segs: [...segs, name] }
}

/** The page URL that opens `segs` (an object's path from the store root):
 * its directory is the drill path, its basename the `open` param; the
 * page's other params (scan, color, scope) ride along. */
export function openHref(storePath: string, segs: string[], search: string): { pathname: string; search: string } {
  const base = storePath === '/' ? '' : storePath
  const dir = segs.slice(0, -1)
  const q = new URLSearchParams(search)
  q.set('open', segs[segs.length - 1])
  return { pathname: dir.length ? `${base}/${dir.join('/')}` : storePath, search: `?${q}` }
}

/** The deployment's scan-store proxy (one bucket, a prefix allow-list). */
export const FILES_API = '/v1/files'
/** The scanned-bucket object proxy: `/v1/objects/<bucket>`. */
export const OBJECTS_API = '/v1/objects'

/** What `/api/store` reports for a deployment's `/v1/files` proxy: the one
 * bucket it reads (`<scheme>://<bucket>`) and the key prefixes it allows —
 * plus, for a member, the scanned buckets `/v1/objects` serves whole. */
export interface ProxyInfo {
  uri: string
  prefixes: string[]
  objectBuckets?: string[]
}

/** Where an opened object's bytes come from:
 * - `public`: the bucket is publicly readable at a base URL (the store's
 *   `objectBases`): the browser range-reads `<base>/<key>` directly.
 * - `proxy`: a gated proxy at `api` reads it: the scan-store proxy
 *   (`/v1/files`, its bucket, under an allowed prefix) or, for a scanned
 *   bucket the deployment serves to members, `/v1/objects/<bucket>`.
 * - `none`: neither — the panel shows the object's size and dates only. */
export type ObjectSource =
  | { kind: 'public'; base: string; key: string }
  | { kind: 'proxy'; key: string; api: string }
  | { kind: 'none'; bucket: string; key: string }

const proxyBucket = (uri: string): string | null => uri.match(/^[a-z0-9]+:\/\/([^/]+)/)?.[1] ?? null

export function objectSource(store: Pick<Store, 'objectBases'>, segs: string[], proxy: ProxyInfo | null | undefined): ObjectSource {
  const [bucket, ...rest] = segs
  const key = rest.join('/')
  const base = store.objectBases?.[bucket]
  if (base) return { kind: 'public', base: base.replace(/\/+$/, ''), key }
  if (proxy && proxyBucket(proxy.uri) === bucket && proxy.prefixes.some(p => key.startsWith(p))) return { kind: 'proxy', key, api: FILES_API }
  if (proxy?.objectBuckets?.includes(bucket)) return { kind: 'proxy', key, api: `${OBJECTS_API}/${bucket}` }
  return { kind: 'none', bucket, key }
}

/** A public object's URL: each key segment percent-encoded, `/` kept. */
export const publicUrl = (base: string, key: string): string =>
  `${base}/${key.split('/').map(encodeURIComponent).join('/')}`

/** Where a retired `/files/<splat>` link lands. The page browsed the
 * deployment's proxy bucket (`proxy.uri`); a configured store that scans that
 * bucket shows the same key as `<store path>/<bucket>/<key>` — a directory
 * (a `/`-terminated splat, or the empty one) as that drill, an object opened
 * under its directory. No store scans the bucket (or the proxy is unknown):
 * no redirect, so the route can show a 404. The old link's query (file-tree's
 * paging) doesn't carry over. */
export function filesRedirect(splat: string, proxy: ProxyInfo | null, stores: Pick<Store, 'path' | 'buckets'>[]): { pathname: string; search: string } | null {
  const bucket = proxy ? proxyBucket(proxy.uri) : null
  const target = bucket ? stores.find(s => s.buckets.includes(bucket)) : undefined
  if (!target || !bucket) return null
  let decoded: string
  try {
    decoded = decodeURIComponent(splat)
  } catch {
    return null
  }
  const segs = [bucket, ...decoded.split('/').filter(Boolean)]
  const isDir = splat === '' || splat.endsWith('/')
  if (isDir) {
    const base = target.path === '/' ? '' : target.path
    return { pathname: `${base}/${segs.join('/')}`, search: '' }
  }
  return openHref(target.path, segs, '')
}

/** The staging / assignment prefix for a row: an object is its own key, a
 * directory its `/`-terminated prefix (so `a/b/` never matches `a/bc`). */
export const actionPrefix = (uri: string, k: 'file' | 'dir' | undefined): string => (k === 'file' ? uri : `${uri}/`)
