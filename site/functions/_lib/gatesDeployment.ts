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

const STAFF = { listOn: 403, listOff: 'pass' } as const

/**
 * Gates only this deployment's routes sit behind, beside the audit's shared vocabulary (which they can't redefine):
 * gate -> its outcome per identity, in the audit's `IDENTITIES` order (anonymous, read-only link, viewer, staff not
 * listed, listed admin, admin grant, agent token). Each must be some route's. A share link's scopes reach none of them.
 */
export const DEPLOYMENT_GATES: Record<string, readonly [DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell, DeploymentCell]> = {
  // An admin's email session: no grant, whatever its scopes (`_lib/devices.ts`, `_lib/laptopFiles.ts`). On
  // `/api/hosts/<id>`, the host's own device token passes too; none of the audit's identities holds one.
  'admin-session': [401, 403, 403, STAFF, 'pass', 403, 403],
}

export const DEPLOYMENT_ROUTES: Record<string, DeploymentCase[]> = {
  // "Open in disky": a signed-in person mints their own hand-off, same-origin only (`_lib/appLink.ts`).
  'api/app-link.ts': [{ method: 'POST', path: '/api/app-link', gate: 'session', sameOrigin: true }],
  'api/executor-status.ts': [{ method: 'GET', path: '/api/executor-status', gate: 'admin' }],
  // A host's own device token, or an admin's session (`_lib/devices.ts` `caller`).
  'api/hosts/[id].ts': [
    { method: 'GET', path: '/api/hosts/nas', gate: 'admin-session' },
    { method: 'DELETE', path: '/api/hosts/nas', gate: 'admin-session' },
  ],
  // The host's own device token only: none of the audit's identities.
  'api/hosts/[id]/uploads.ts': [{ method: 'POST', path: '/api/hosts/nas/uploads', gate: 'nobody' }],
  'api/hosts/index.ts': [
    // Members: an email session holding the base scope (host labels are private).
    { method: 'GET', path: '/api/hosts', gate: 'session' },
    // Enrolling a Mac: an admin's same-origin session.
    { method: 'POST', path: '/api/hosts', gate: 'admin-session', sameOrigin: true },
  ],
  // No session (or a grant): a redirect to sign in, back here; a session: its hand-off page.
  'auth/app-handoff.ts': [{ method: 'GET', path: '/auth/app-handoff', gate: 'public' }],
  // The link's token is the credential; a dead or missing one redirects to `/signin`.
  'auth/app-link.ts': [{ method: 'GET', path: '/auth/app-link', gate: 'public' }],
  'v1/laptop-files/[[path]].ts': [
    // `requireAdmin`, then `serveLaptopFile` refuses any grant: an admin's session only.
    { method: 'GET', path: '/v1/laptop-files/get?path=a', gate: 'admin-session' },
    // A secondary store (the NAS) stays metadata-only: no file bytes, for anyone.
    { method: 'GET', path: '/v1/laptop-files/get?store=nas&path=a', gate: 'unconfigured' },
  ],
}

/**
 * What a path-restricted share link gets from each of those cases a share link's scopes reach (gates `public`,
 * `signed-in`, `viewer`, `stager`, `assigner`, `email`, `named-grant`), keyed `'<METHOD> <path without its query>'`:
 * `blocked` (the 403 a restricted link gets, by `RESTRICTED_BLOCKED` or the route), `filtered` (what the link hides
 * is left out), `pathless` (the answer names no scanned path) or `anonymous` (a public route: the link earns nothing
 * more). A reachable case without an answer fails the audit.
 */
export const DEPLOYMENT_RESTRICTED: Record<string, string> = {
  'GET /auth/app-handoff': 'anonymous',
  'GET /auth/app-link': 'anonymous',
}
