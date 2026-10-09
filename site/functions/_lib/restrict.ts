/**
 * Path scopes (specs/path-scopes.md): a share link whose grant carries
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
 *  bucket bytes and D1-held path lists can't be filtered per path. */
export const RESTRICTED_BLOCKED = [
  '/api/path-index', '/v1', '/api/plans', '/api/actions', '/api/db', '/api/owners',
  '/api/estate', '/api/assignments', '/api/sweep', '/api/plan-sweep', '/api/og',
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

/** Whether `path` (store-relative) is strictly below a hidden dir. */
export const isHidden = (path: string, r: Restriction): boolean => {
  const p = norm(path)
  return r.hide.some(h => p.startsWith(`${h}/`))
}

/** Whether `path` is a hidden dir itself (shown, but as a leaf). */
const isHideRoot = (path: string, r: Restriction): boolean => r.hide.includes(norm(path))

const join = (a: string, b: string): string => (a ? (b ? `${a}/${b}` : a) : b)

/** `node` (at store path `path`) with hidden dirs' contents removed: a
 *  hidden dir keeps its own sizes but loses its children and child counts. */
export function restrictTree(node: ViewNode, path: string, r: Restriction): ViewNode {
  if (isHideRoot(path, r)) {
    const { c: _c, f: _f, nc: _nc, nd: _nd, ...leaf } = node
    return leaf as ViewNode
  }
  if (!node.c) return node
  const c = node.c
    .filter(ch => !isHidden(join(path, ch.n), r))
    .map(ch => restrictTree(ch, join(path, ch.n), r))
  return { ...node, c }
}

/** The search fields of a subtree/diff body (`matches`, `matched`,
 *  `excluded`), hidden paths removed. */
function restrictSearch<T extends Record<string, unknown>>(body: T, r: Restriction): T {
  const out: Record<string, unknown> = { ...body }
  if (Array.isArray(body.matches)) out.matches = (body.matches as string[]).filter(p => !isHidden(p, r))
  if (Array.isArray(body.matched)) out.matched = (body.matched as { path: string }[]).filter(m => !isHidden(m.path, r))
  if (Array.isArray(body.excluded)) out.excluded = (body.excluded as string[]).filter(p => !isHidden(p, r))
  return out as T
}

/** A `/api/subtree` body for a restricted viewer. */
export function restrictSubtree(body: { path: string; tree: ViewNode } & Record<string, unknown>, r: Restriction) {
  return restrictSearch({ ...body, tree: restrictTree(body.tree, body.path, r) }, r)
}

/** A `/api/diff` body for a restricted viewer: rows are relative to `path`. */
export function restrictDiff(body: { path: string; rows: { p: string }[] } & Record<string, unknown>, r: Restriction) {
  return restrictSearch({ ...body, rows: body.rows.filter(row => !isHidden(join(norm(body.path), row.p), r)) }, r)
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

/** 403 for a route a restricted link can't use (raw bytes, D1 path lists). */
export const restrictedForbidden = (): Response =>
  new Response(JSON.stringify({ error: 'forbidden for a path-restricted link' }) + '\n', { status: 403, headers: { 'content-type': 'application/json; charset=utf-8' } })
