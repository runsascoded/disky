/**
 * URI-scheme / route-type helpers.
 *
 * disk-tree URLs come in two flavors:
 *   - `/file/<local-path>` for local filesystem scans
 *   - `/<scheme>/<host-and-path>` for URI-scheme scans (s3/gcs/r2/ssh)
 *
 * The URIs on the backend / API use the canonical form: `/local/path` or
 * `<scheme>://host/path`. Keep every scheme<->route-type mapping in this file
 * so adding a new scheme (e.g. `azure`) is a one-line edit.
 */

/** All URI schemes recognized in disk-tree URLs. `file` denotes a local path. */
export type RouteType = 'file' | 's3' | 'gcs' | 'r2' | 'ssh'

/** Schemes rendered as `<scheme>://…` URIs (everything except `file`). */
export const URI_SCHEMES: readonly Exclude<RouteType, 'file'>[] = ['s3', 'gcs', 'r2', 'ssh']

/** Detect the route type from a `location.pathname` (e.g. `/s3/bucket`). */
export function detectRouteType(pathname: string): RouteType {
  for (const scheme of URI_SCHEMES) {
    if (pathname === `/${scheme}` || pathname.startsWith(`/${scheme}/`)) return scheme
  }
  return 'file'
}

/**
 * Reconstruct a URI ('gcs://bucket/prefix' or '/local/path') from a route
 * type and the '*' segments captured by react-router's splat route.
 */
export function segmentsToUri(routeType: RouteType, segments: string): string {
  return routeType === 'file' ? `/${segments}` : `${routeType}://${segments}`
}

/** URI → browser path. `s3://foo/bar` → `/s3/foo/bar`, `/x/y` → `/file/x/y`. */
export function uriToPath(uri: string): string {
  for (const scheme of URI_SCHEMES) {
    const prefix = `${scheme}://`
    if (uri.startsWith(prefix)) return `/${scheme}/${uri.slice(prefix.length)}`
  }
  return `/file${uri}`
}

/**
 * Prefix for building child-links under `uri` (no trailing slash).
 * `/`         → `/file`
 * `s3://`     → `/s3`
 * `s3://b/x/` → `/s3/b/x`
 */
export function childLinkPrefix(uri: string): string {
  const path = uriToPath(uri).replace(/\/+$/, '')
  return path || '/file'
}

/**
 * Reconstruct a URI from a `/compare/<scheme>/<segments>` pathname.
 * The caller has already stripped the leading `/compare`. Returns
 * `('/', 'file')` for the empty case.
 */
export function comparePathToUri(pathAfterCompare: string): { uri: string; routeType: RouteType } {
  for (const scheme of URI_SCHEMES) {
    if (pathAfterCompare === `/${scheme}` || pathAfterCompare.startsWith(`/${scheme}/`)) {
      const rest = pathAfterCompare.slice(scheme.length + 1) // strip '/<scheme>'
      const uri = rest ? `${scheme}:/${decodeURIComponent(rest)}` : `${scheme}://`
      return { uri, routeType: scheme }
    }
  }
  if (pathAfterCompare.startsWith('/file')) {
    return { uri: decodeURIComponent(pathAfterCompare.replace(/^\/file/, '') || '/'), routeType: 'file' }
  }
  return { uri: decodeURIComponent(pathAfterCompare || '/'), routeType: 'file' }
}

/**
 * Per-scheme UI descriptor — the single registry `Breadcrumbs` / `Header` /
 * the bucket-list page read from, so no view component special-cases a scheme
 * (spec `union-of-roots.md`). Adding a scheme stays a one-line edit here.
 */
export interface SchemeDesc {
  /** Breadcrumb / nav label, e.g. `r2://` or `/`. */
  label: string
  /**
   * Where this scheme's top breadcrumb crumb (and any Header nav link) points:
   * a *dedicated* bucket-list page (`/s3`) if it has one, else the `/` union
   * landing, else `null` (render the scheme as plain text, no up-link).
   */
  landing: string | null
}

export const SCHEMES: Record<RouteType, SchemeDesc> = {
  file: { label: '/', landing: '/file' },
  // Cloud schemes get a dedicated per-scheme landing (`/r2` = the R2 buckets,
  // etc.) so a multi-cloud deployment can disambiguate. A *single*-cloud
  // deployment (mgu gcs/cw-s3) that needs no disambiguator would set its one
  // scheme's landing to `/`. `ssh` has hosts, not buckets — no landing page.
  s3: { label: 's3://', landing: '/s3' },
  gcs: { label: 'gcs://', landing: '/gcs' },
  // This deployment is effectively single-cloud (R2 only — no GCS to demo), and
  // the host is already `r2.rbw.sh`, so `r2` needs no `/r2` disambiguator: its
  // landing IS `/` (the union root). A multi-cloud deployment would set this to
  // `/r2` and register a sibling `/gcs`, etc. (spec `union-of-roots.md`).
  r2: { label: 'r2://', landing: '/' },
  ssh: { label: 'ssh://', landing: '/' },
}

/** The path a scheme's top breadcrumb crumb links up to (`SCHEMES[rt].landing`). */
export function schemeLanding(rt: RouteType): string | null {
  return SCHEMES[rt].landing
}

/** `true` for `'/'` or `<scheme>://` (empty-scan-root placeholder URIs). */
export function isSchemeRoot(uri: string): boolean {
  if (uri === '/') return true
  return URI_SCHEMES.some(s => uri === `${s}://`)
}

/**
 * Whether a route's scheme has a delete-capable backend at all (`backend_for`
 * can remove it): local `gfind`/`rm`, `ssh`, `S3Backend.delete` (`aws s3 rm`,
 * R2 via endpoint). `gcs` has no DT delete backend yet. *How* a delete runs —
 * inline (`sync`) or via the staged queue — is the deployment's `deleteApproval`
 * policy (`useDeleteMethod`), not the scheme.
 */
export function supportsDelete(routeType: RouteType): boolean {
  return routeType === 'file' || routeType === 'ssh' || routeType === 's3' || routeType === 'r2'
}
