/**
 * The gate audit's rows (`functions/gates.test.ts`) for the routes only this deployment's branch carries: route file
 * (relative to `functions/`) -> one case per distinct gate branch, in the audit's vocabulary (`public`, `viewer`,
 * `stager`, `admin`, ...; a name it doesn't know fails it). `cloud` carries none. A deployment branch declares its
 * own routes here, so the audit's shared table merges without conflict; a route file declared nowhere fails the audit.
 */
export interface DeploymentCase {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** Path and query. */
  path: string
  gate: string
  /** Vars and bindings the route needs to take this branch. */
  env?: Record<string, unknown>
}

const get = (path: string, gate: string, env?: DeploymentCase['env']): DeploymentCase => ({ method: 'GET', path, gate, ...(env ? { env } : {}) })

/** "Sign in with OA" configured (`oaLoginConfig`): the broker, and this deployment's client on it. */
const OA_LOGIN = { OA_LOGIN_ISSUER: 'https://login.example.test', OA_LOGIN_CLIENT_ID: 'client-id', OA_LOGIN_CLIENT_SECRET: 'client-secret' }

export const DEPLOYMENT_ROUTES: Record<string, DeploymentCase[]> = {
  // The session monitor's scheduler call: past `MONITOR_END_MS` (2026-10-21) a quiet `{ status: 'expired' }` for
  // everyone, before its gate. The gate behind it, the monitor's own scope (`MONITOR_SCOPE`, which no viewer, admin
  // or agent token holds), is asserted under a clock before the end in `_lib/sessionMonitor.test.ts`.
  'api/session-monitor.ts': [{ method: 'POST', path: '/api/session-monitor', gate: 'unconfigured' }],
  // "Sign in with OA" (`specs/oa-login-rp.md`): sign-in itself; a 404 until the deployment has its broker client.
  'auth/oa.ts': [get('/auth/oa', 'public', OA_LOGIN), get('/auth/oa', 'unconfigured')],
  'auth/oa/callback.ts': [get('/auth/oa/callback', 'public', OA_LOGIN), get('/auth/oa/callback', 'unconfigured')],
  // Whether the wall shows the button.
  'auth/oa/client.ts': [get('/auth/oa/client', 'public')],
  // A deletion run's unfurl for a crawler: its state and object counts, no prefixes, sizes or names.
  'runs/[[id]].ts': [get('/runs/plan-1/20261104T125245Z', 'public')],
  // That run's card, a branch of the card route the shared table declares: counts only, for any fetcher (`_lib/og/run.ts`).
  'og/[[path]].ts': [get('/og/map.png', 'public'), get('/og/run.png?id=plan-1/20261104T125245Z', 'public')],
}

/**
 * What a path-restricted share link gets from each of those cases a share link's scopes reach (gates `public`,
 * `signed-in`, `viewer`, `stager`, `assigner`, `email`, `named-grant`), keyed `'<METHOD> <path without its query>'`:
 * `blocked` (the 403 a restricted link gets, by `RESTRICTED_BLOCKED` or the route), `filtered` (what the link hides
 * is left out), `pathless` (the answer names no scanned path) or `anonymous` (a public route: the link earns nothing
 * more). A reachable case without an answer fails the audit.
 */
export const DEPLOYMENT_RESTRICTED: Record<string, string> = {
  // Sign-in, for whoever arrives: a link's credential earns nothing here.
  'GET /auth/oa': 'anonymous',
  'GET /auth/oa/callback': 'anonymous',
  'GET /auth/oa/client': 'anonymous',
  // A deletion run's unfurl and card: its state and counts, for any fetcher; no prefixes, sizes or names.
  'GET /runs/plan-1/20261104T125245Z': 'anonymous',
  'GET /og/run.png': 'anonymous',
}
