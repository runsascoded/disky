/**
 * Path scopes: a share link whose grant carries
 * `<tag>:hide:<prefix>` sees `<prefix>` as one cell (its own row, with its
 * aggregate size) and nothing strictly below it.
 *
 * `<tag>` names the store the restriction applies to: the deployment's base
 * scope for the primary store (`laptop`), a secondary store's key (`nas`).
 *
 * Enforcement filters each route's response on the way out, after any cache
 * read, so the readers and shared caches stay restriction-free: a cached
 * unrestricted body never reaches a restricted viewer as-is, and a filtered
 * body is never written to a shared key. Unrestricted viewers
 * (`restrictionFor` → null) get the untouched response.
 */
import { type Env, type Identity, baseScope, identify } from './auth.js'
import { isPrimary, storeKey } from './stores.js'
import type { ViewNode } from './view.js'

export interface Restriction {
  /** Store-relative dirs whose contents are hidden (no leading/trailing `/`). */
  hide: string[]
}

/** The scope tag a store's path scopes carry. */
export const restrictTag = (env: Env): string => (isPrimary(env) ? baseScope(env) : storeKey(env))

/** A path-restriction scope on any store (`<tag>:hide:<prefix>`). */
export const isPathScope = (scope: string): boolean => /^[a-z0-9-]+:hide:/.test(scope)

/** Routes a path-restricted link can't use at all (`_middleware.ts`): raw
 *  bucket bytes, D1-held path lists, and answers naming paths that no route
 *  filters (name search summaries and covers, the static-index bench, the
 *  append stores' health, scan-run outputs, the digest's image: a drawn diff
 *  of whatever path its caller names). */
export const RESTRICTED_BLOCKED = [
  '/api/path-index', '/v1', '/api/plans', '/api/actions', '/api/db', '/api/owners',
  '/api/estate', '/api/assignments', '/api/sweep', '/api/plan-sweep', '/api/og',
  '/api/name-summary', '/api/name-summary-registry', '/api/static-bench', '/api/filter-cover', '/api/name-matches', '/api/health', '/api/scan-runs',
  '/api/digest-image',
]

export const blockedForRestricted = (pathname: string): boolean =>
  RESTRICTED_BLOCKED.some(p => pathname === p || pathname.startsWith(`${p}/`))

/** The middleware's guard: a 403 for a path-restricted link on a blocked
 *  route, else null (any store's restriction counts — conservative). */
export async function restrictGate(request: Request, env: Env): Promise<Response | null> {
  if (!blockedForRestricted(new URL(request.url).pathname)) return null
  const id = await identify({ request, env } as never)
  return id?.via === 'grant' && id.scopes.some(isPathScope) ? restrictedForbidden() : null
}

/** Whether a restricted viewer may read snapshot file `key` (`/data`, a key
 *  under the store's `own` snapshot dir; `rules.json`, the identity roster, is
 *  the caller's to filter: `restrictRules`): a scan's `meta.json` (totals, owners, classes), and
 *  its whole-fleet `age.json` (rows per top-level dir) unless a hide prefix is
 *  itself top-level — its row would break down what the link hides. Anything
 *  else a scan publishes (`lifecycle.json`'s rule prefixes, and whatever a job
 *  adds later) is refused: unfiltered, it could name hidden paths. */
export function dataAllowed(key: string, own: string, r: Restriction): boolean {
  const file = key.startsWith(own) ? key.slice(own.length).split('/').slice(1).join('/') : ''
  if (file === 'meta.json') return true
  if (file === 'age.json') return !r.hide.some(h => !h.includes('/'))
  return false
}

/** `rules.json` for a restricted viewer: the roster (`users`) whole, and the attribution rules (`prefix_owners`:
 *  a prefix URI, its owner, a note) without those for a prefix strictly below a hidden dir. */
export function restrictRules<T extends { prefix_owners?: { prefix: string }[] }>(rules: T, r: Restriction): T {
  if (!Array.isArray(rules.prefix_owners)) return rules
  return { ...rules, prefix_owners: rules.prefix_owners.filter(row => !isHidden(row.prefix.replace(/^[a-z0-9]+:\/\/\/*/, ''), r)) }
}

/** A prefix as a scope carries it: auth stores a grant's scopes space- and
 *  comma-separated, so those (and `%`) are percent-encoded; `/` stays. */
export const encodePrefix = (prefix: string): string => encodeURI(norm(prefix)).replace(/,/g, '%2C')
const decodePrefix = (s: string): string => {
  try { return decodeURIComponent(s) } catch { return s }
}

/** The scope that hides `prefix` on the store tagged `tag`. */
export const hideScope = (tag: string, prefix: string): string => `${tag}:hide:${encodePrefix(prefix)}`

const norm = (p: string): string => p.replace(/^\/+|\/+$/g, '')

/** The viewer's restriction on this store, or null (unrestricted). Only a
 *  grant (share link) can carry one; email sessions never do. */
export function restrictionFor(id: Identity | null, env: Env): Restriction | null {
  if (!id || id.via !== 'grant') return null
  const pre = `${restrictTag(env)}:hide:`
  const hide = [...new Set(id.scopes.filter(s => s.startsWith(pre)).map(s => norm(decodePrefix(s.slice(pre.length)))).filter(Boolean))]
  return hide.length ? { hide } : null
}

/** Every hide prefix a grant carries, whatever store it names, as one restriction; null without any. For an answer
 *  that isn't one store's (a union view's notes: a path there is some member's): over-hiding is the safe side. */
export function restrictionAnyStore(id: Identity | null): Restriction | null {
  if (!id || id.via !== 'grant') return null
  const hide = [...new Set(id.scopes.filter(isPathScope).map(s => norm(decodePrefix(s.slice(s.indexOf(':hide:') + ':hide:'.length)))).filter(Boolean))]
  return hide.length ? { hide } : null
}

/** Whether `path` (store-relative) is strictly below a hidden dir. */
export const isHidden = (path: string, r: Restriction): boolean => {
  const p = norm(path)
  return r.hide.some(h => p.startsWith(`${h}/`))
}

/** Whether `path` is a hidden dir itself (shown, but as a leaf). */
export const isHideRoot = (path: string, r: Restriction): boolean => r.hide.includes(norm(path))

/** Whether a read of `path` under an owner, class or lens scope would break down what `r` hides: `path` is a hidden dir
 *  or above one. Its hidden cell would be the scope's share of the dir's contents, and its total, less its visible
 *  children's, the same; a sum can't be filtered on the way out, so the routes refuse it (`restrictedScopeRefused`). An
 *  unscoped total reveals only the dir's size, which the link shows. */
export const scopeReachesHidden = (path: string, r: Restriction): boolean => {
  const p = norm(path)
  return r.hide.some(h => !p || h === p || h.startsWith(`${p}/`))
}

const join = (a: string, b: string): string => (a ? (b ? `${a}/${b}` : a) : b)

/** `node` (at store path `path`) with hidden dirs' contents removed: a
 *  hidden dir keeps its own sizes but loses its children and child counts. */
export function restrictTree(node: ViewNode, path: string, r: Restriction): ViewNode {
  if (isHideRoot(path, r)) {
    const { children: _c, folded: _f, ...leaf } = node
    return leaf
  }
  if (!node.children) return node
  const children = node.children
    .filter(ch => !isHidden(join(path, ch.name), r))
    .map(ch => restrictTree(ch, join(path, ch.name), r))
  return { ...node, children }
}

/** A `/api/subtree` body for a restricted viewer. */
export function restrictSubtree<T extends { path: string; tree: ViewNode }>(body: T, r: Restriction): T {
  return { ...body, tree: restrictTree(body.tree, body.path, r) }
}

/** A `/api/diff` body for a restricted viewer: rows are relative to `path`. */
export function restrictDiff<T extends { path: string; rows: { path: string }[] }>(body: T, r: Restriction): T {
  return { ...body, rows: body.rows.filter(row => !isHidden(join(norm(body.path), row.path), r)) }
}

/** A JSON response re-emitted through `fn` (same status and headers); a
 *  non-OK response passes through untouched. */
export async function filterJson<T>(res: Response, fn: (body: T) => unknown): Promise<Response> {
  if (!res.ok) return res
  const body = JSON.stringify(fn(await res.json() as T))
  const headers = new Headers(res.headers)
  headers.delete('content-length')
  return new Response(body, { status: res.status, headers })
}

const plain = (msg: string, status: number): Response => new Response(msg, { status })

/** 404 for a request naming a hidden path (as if it didn't exist). */
export const hiddenNotFound = (): Response => plain('path not found', 404)

const refused = (error: string, code: string): Response =>
  new Response(JSON.stringify({ error, code }) + '\n', { status: 403, headers: { 'content-type': 'application/json; charset=utf-8' } })

/** 403 for a name filter: its sums count hidden hits. Its `code` is a refusal the client states as its reason (`filterCaps.ts`). */
export const restrictedSearchRefused = (): Response => refused('search isn’t available on this link', 'restricted-search')

/** 403 for an owner, class or lens scope at or above a hidden dir (`scopeReachesHidden`); a refusal, as above. */
export const restrictedScopeRefused = (): Response => refused('this owner, class or user view isn’t available here on this link', 'restricted-scope')

/** 403 for a route a restricted link can't use (raw bytes, D1 path lists). */
export const restrictedForbidden = (): Response =>
  new Response(JSON.stringify({ error: 'forbidden for a path-restricted link' }) + '\n', { status: 403, headers: { 'content-type': 'application/json; charset=utf-8' } })
