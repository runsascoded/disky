/**
 * The gate audit's rows (`functions/gates.test.ts`) for the routes only this deployment's branch carries: route file
 * (relative to `functions/`) -> one case per distinct gate branch, in the audit's vocabulary (`public`, `viewer`,
 * `stager`, `admin`, ..., or one of `DEPLOYMENT_GATES`; a name it doesn't know fails it). `cloud` carries none. A
 * deployment branch declares its own routes here, so the audit's shared table merges without conflict; a route file
 * declared nowhere fails the audit.
 */
export interface DeploymentCase {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** Path and query. */
  path: string
  gate: string
  /** Vars and bindings the route needs to take this branch. */
  env?: Record<string, unknown>
  /** A same-origin browser request: for a route that refuses a cross-site request before any gate. */
  sameOrigin?: boolean
}

/** An outcome (`401`, `403`, `pass`), or the staff cell: refused with `ADMIN_LIST` on, passed with it off. */
export type DeploymentCell = 401 | 403 | 'pass' | { readonly listOn: 403; readonly listOff: 'pass' }

/**
 * Gates only this deployment's routes sit behind, beside the audit's shared vocabulary (which they can't redefine):
 * gate -> its outcome per identity, in the audit's `IDENTITIES` order (anonymous, read-only link, viewer, staff not
 * listed, listed admin, admin grant, agent token). Each must be some route's. A share link's scopes reach none of them.
 */
export const DEPLOYMENT_GATES: Record<string, readonly [DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell]> = {}

export const DEPLOYMENT_ROUTES: Record<string, DeploymentCase[]> = {}

/**
 * What a path-restricted share link gets from each of those cases a share link's scopes reach (gates `public`,
 * `signed-in`, `viewer`, `stager`, `assigner`, `email`, `named-grant`), keyed `'<METHOD> <path without its query>'`:
 * `blocked` (the 403 a restricted link gets, by `RESTRICTED_BLOCKED` or the route), `filtered` (what the link hides
 * is left out), `pathless` (the answer names no scanned path) or `anonymous` (a public route: the link earns nothing
 * more). A reachable case without an answer fails the audit.
 */
export const DEPLOYMENT_RESTRICTED: Record<string, string> = {}
