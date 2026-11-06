/**
 * The gate audit: every Pages Functions route, the gate it sits behind, and what each kind of caller gets from it.
 *
 * `ROUTES` declares, per route file, one case per distinct gate branch (a catch-all or an all-method `onRequest`
 * gates per method and sub-path). A route file with no entry, an entry with no file, an exported handler no case
 * reaches, or a case another file would serve fails the enumeration tests, so a new route can't ship without
 * saying who may call it. A route only a deployment's branch carries is declared in `_lib/gatesDeployment.ts`, the
 * one file of this audit that branch edits.
 *
 * Each case is then called, through the route's own exported handler and a real gate (`gateFor` over an in-memory
 * D1), as seven identities, and the outcomes (`401`, `403`, or `pass`: anything else, a throw included, since a
 * handler past its gate may fail on a binding this env doesn't have) must equal `EXPECT[gate]`. The matrix runs
 * twice: with `ADMIN_LIST` (admin is the explicit list; the staff domain alone isn't) and without (staff are admins).
 *
 * A path-restricted share link (a grant carrying `<tag>:hide:<prefix>`) is its own dimension: every case a share
 * link's scopes can reach (`LINK_GATES`) declares in `RESTRICTED` what such a link gets from it (`blocked`, `filtered`,
 * `pathless`, `anonymous`), so a new route can't become readable by one without saying whether it could name what the
 * link hides. Each is called as the middleware would (`restrictGate`, then the handler): `blocked` must be the 403 a
 * restricted link gets, the rest must answer it as they answer the same link without its restriction. What a
 * `filtered` route leaves out is its own tests' to assert (`_lib/restrict.test.ts`, `api/annotations.test.ts`,
 * `api/restrictedFilter.test.ts`, `api/diff/level.test.ts`, `_lib/dataProxy.test.ts`).
 *
 * Not covered here: `PUBLIC_READ` deployments (anonymous passes the viewer gates), a secondary store's extra scope
 * (`STORE_SCOPE`), and a grant holding `admin` beside a hide scope. Such a grant isn't modelled as restricted because
 * no hide scope confines it: it can mint itself an unrestricted link (`LINK_GATES`), and `gateFor`'s store refuses one
 * with `ADMIN_LIST` on. The filters do still apply to it where they run; they just aren't what this audit relies on.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { ADMIN_GRANT_REFUSED, type Env, gateFor, identify } from './_lib/auth'
import { DEPLOYMENT_RESTRICTED, DEPLOYMENT_ROUTES } from './_lib/gatesDeployment'
import { hideScope, restrictGate } from './_lib/restrict'
import { sqliteD1, withAdminEmails, withLedger } from './_lib/testD1'

// The card renderer's wasm (`og/serve.ts`): nothing here draws a card.
vi.mock('./_lib/og/vendor/resvg.wasm', () => ({ default: new Uint8Array() }))

// ---- the vocabulary ------------------------------------------------------------------------------------------------

/** What stands between a caller and a route. */
type Gate =
  /** No identity needed. */
  | 'public'
  /** Refused before any gate, the same for everyone: the deployment hasn't the feature (a 404, 409 or 503). */
  | 'unconfigured'
  /** Any identity the gate authenticates, whatever its scopes (`identify`, the auth package's `auth`). */
  | 'signed-in'
  /** `requireViewer`: the base scope or its read-only tier. */
  | 'viewer'
  /** `requireStager`, or a check of the bare base scope: a read-only link can't. */
  | 'stager'
  /** `requireAssigner` (the assign scope, or admin), then an email on the identity (the ledger's actor). */
  | 'assigner'
  /** The `admin` scope: `requireAdmin`, `requireScope(ADMIN_SCOPE)`, the auth package's admin routes. */
  | 'admin'
  /** An email session only: no grant, whatever its scopes or email. */
  | 'session'
  /** An email session, or a grant holding `admin`. */
  | 'session-or-admin'
  /** Any identity carrying an email; one without (an unnamed link) reads as signed out. */
  | 'email'
  /** `requireViewer`, then one grant by id (`STAGED_CARD_GRANT`; here the read-only link's). */
  | 'named-grant'
  /** Signed in, and refused whoever asks (a read-only table's write). */
  | 'nobody'
  /** Slack's request signature; no site identity is consulted. */
  | 'slack-signature'
  /** A Google id_token in the body; no site identity is consulted. */
  | 'google-credential'

const IDENTITIES = ['anonymous', 'read-only link', 'viewer', 'staff, not listed', 'listed admin', 'admin grant', 'agent token'] as const
type Who = typeof IDENTITIES[number]
type Outcome = 401 | 403 | 'pass'

const P = 'pass'
/** The one cell that depends on the mode: staff are admins by their domain, unless admin is the list alone. */
const STAFF = { listOn: 403, listOff: P } as const
type Cell = Outcome | typeof STAFF

/** Gate × identity → outcome, in `IDENTITIES` order. */
const EXPECT: Record<Gate, readonly [Cell, Cell, Cell, Cell, Cell, Cell, Cell]> = {
  //                     anonymous  read-only  viewer  staff,  listed  admin  agent
  //                                link               not     admin   grant  token
  //                                                   listed
  'public':             [P,         P,         P,      P,      P,      P,     P],
  'unconfigured':       [P,         P,         P,      P,      P,      P,     P],
  'signed-in':          [401,       P,         P,      P,      P,      P,     P],
  'viewer':             [401,       P,         P,      P,      P,      P,     P],
  'stager':             [401,       403,       P,      P,      P,      P,     P],
  'assigner':           [401,       403,       P,      P,      P,      403,   P],
  'admin':              [401,       403,       403,    STAFF,  P,      P,     403],
  'session':            [401,       403,       P,      P,      P,      403,   403],
  'session-or-admin':   [401,       403,       P,      P,      P,      P,     403],
  'email':              [401,       401,       P,      P,      P,      401,   P],
  'named-grant':        [401,       P,         403,    403,    403,    403,   403],
  'nobody':             [401,       403,       403,    403,    403,    403,   403],
  'slack-signature':    [401,       401,       401,    401,    401,    401,   401],
  'google-credential':  [401,       401,       401,    401,    401,    401,   401],
}

// ---- the routes ----------------------------------------------------------------------------------------------------

interface World {
  env: Env
  /** Each identity's request headers. */
  headers: Record<Who, Record<string, string>>
  /** The read-only link's grant id (`named-grant`). */
  readOnlyGrant: string
  /** A share link with all a link can hold short of admin (the base scope and assign), hiding `HIDDEN` (`restricted`),
   *  and the same link without the restriction (`open`): their request headers. */
  links: { restricted: Record<string, string>; open: Record<string, string> }
}

type Vars = Record<string, unknown>
interface Case {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** Path and query, on `ORIGIN`. */
  path: string
  gate: Gate
  /** Vars and bindings on top of the world's env: what the deployment needs for the route to take this branch. */
  env?: Vars | ((w: World) => Vars)
}

const route = (method: Case['method']) => (path: string, gate: Gate, env?: Case['env']): Case => ({ method, path, gate, ...(env ? { env } : {}) })
const get = route('GET')
const post = route('POST')
const put = route('PUT')
const patch = route('PATCH')
const del = route('DELETE')

/** A scan-reading route checks its view before its gate: a date it accepts. */
const AT = 'date=2026-10-01'
const SPAN = 'from=2026-09-01&to=2026-10-01'
const STAGING = { STAGING: '1' }
const INTERVALS = { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: { get: async () => null, head: async () => null } }
const GOOGLE = { GOOGLE_CLIENT_ID: 'client-id', GOOGLE_CLIENT_SECRET: 'client-secret' }
/** "Sign in with OA" configured (`oaLoginConfig`): the broker, and this deployment's client on it. */
const OA_LOGIN = { OA_LOGIN_ISSUER: 'https://login.example.test', OA_LOGIN_CLIENT_ID: 'client-id', OA_LOGIN_CLIENT_SECRET: 'client-secret' }
const MAIL = { RESEND_API_KEY: 'resend-key', MAIL_FROM: 'Disky <disky@example.org>' }

const SHARED_ROUTES: Record<string, Case[]> = {
  // An activity post's sender face, named by its own SHA-256.
  'a/[[path]].ts': [get('/a/0000.png', 'public')],
  'api/actions.ts': [get('/api/actions', 'viewer'), post('/api/actions', 'assigner')],
  'api/activity/[[path]].ts': [get('/api/activity/kinds', 'admin'), put('/api/activity/kinds/assign', 'admin'), post('/api/activity/render', 'admin')],
  'api/admin/r2-probe.ts': [get('/api/admin/r2-probe', 'admin')],
  'api/admins.ts': [get('/api/admins', 'admin'), post('/api/admins', 'admin'), patch('/api/admins', 'admin'), del('/api/admins', 'admin')],
  'api/age-pyramid.ts': [get(`/api/age-pyramid?${AT}`, 'viewer')],
  'api/annotations.ts': [get('/api/annotations', 'viewer'), post('/api/annotations', 'admin'), patch('/api/annotations', 'admin'), del('/api/annotations?id=1', 'admin')],
  'api/assignments.ts': [get(`/api/assignments?${AT}`, 'viewer')],
  // `@open-athena/auth`'s `authRoutes`.
  'api/auth/[[path]].ts': [
    get('/api/auth/whoami', 'signed-in'),
    post('/api/auth/exchange', 'public'),
    post('/api/auth/logout', 'public'),
    post('/api/auth/track', 'public'),
    get('/api/auth/profile', 'signed-in'),
    put('/api/auth/profile', 'session'),
    post('/api/auth/request', 'public'),
    get('/api/auth/decide', 'public'),
    post('/api/auth/decide', 'public'),
    get('/api/auth/grants', 'admin'),
    post('/api/auth/grants', 'admin'),
    post('/api/auth/grants/none/revoke', 'admin'),
    get('/api/auth/requests', 'admin'),
    get('/api/auth/allowed', 'admin'),
    post('/api/auth/allowed/sync', 'admin'),
    get('/api/auth/log', 'admin'),
    post('/api/auth/avatar', 'session-or-admin'),
    get('/api/auth/avatar/none', 'signed-in'),
  ],
  'api/cost/[cloud].ts': [get('/api/cost/gcp', 'admin')],
  // Per table: its `readScope` to read, its `writeScope` to write (`_lib/tables.ts`).
  'api/db/[[path]].ts': [
    get('/api/db', 'signed-in'),
    get('/api/db/allowed_emails', 'stager'),
    post('/api/db/allowed_emails', 'admin'),
    patch('/api/db/allowed_emails', 'admin'),
    del('/api/db/allowed_emails', 'admin'),
    get('/api/db/admin_edits', 'admin'),
    post('/api/db/admin_edits', 'nobody'),
    post('/api/db/actions', 'nobody'),
  ],
  'api/diff.ts': [get(`/api/diff?${SPAN}`, 'viewer')],
  // Level diffs read the interval store: without one, a 409 before the gate.
  'api/diff/level.ts': [get(`/api/diff/level?${SPAN}&p=a`, 'viewer', INTERVALS), get(`/api/diff/level?${SPAN}&p=a`, 'unconfigured')],
  'api/digest-image.ts': [post('/api/digest-image', 'viewer')],
  'api/digest-op.ts': [post('/api/digest-op', 'viewer')],
  'api/digest-preview.ts': [get('/api/digest-preview', 'admin')],
  'api/digest-reapply.ts': [post('/api/digest-reapply', 'admin'), get('/api/digest-reapply', 'admin')],
  'api/digest-settings.ts': [get('/api/digest-settings', 'viewer'), put('/api/digest-settings', 'admin')],
  'api/dups.ts': [get(`/api/dups?${AT}`, 'viewer', { DUPS: '1' }), get(`/api/dups?${AT}`, 'unconfigured')],
  'api/estate.ts': [get(`/api/estate?${AT}`, 'viewer')],
  // Deployment config only.
  'api/filter-caps.ts': [get('/api/filter-caps', 'public')],
  'api/filter-cover.ts': [get(`/api/filter-cover?${AT}&q=abc`, 'viewer')],
  'api/filter-scans.ts': [get('/api/filter-scans?q=abc', 'viewer')],
  'api/footer-cache.ts': [get('/api/footer-cache', 'admin'), post('/api/footer-cache', 'admin')],
  'api/health.ts': [get('/api/health', 'viewer')],
  'api/largest.ts': [get(`/api/largest?${AT}`, 'viewer')],
  'api/me.ts': [get('/api/me', 'viewer')],
  'api/name-matches.ts': [get(`/api/name-matches?${AT}&q=abc`, 'viewer')],
  'api/name-summary-registry.ts': [get('/api/name-summary-registry', 'viewer'), post('/api/name-summary-registry', 'viewer')],
  'api/name-summary.ts': [get('/api/name-summary', 'viewer'), post('/api/name-summary', 'viewer')],
  'api/og/[[path]].ts': [post('/api/og/mint', 'stager'), del('/api/og/tokens/none', 'stager'), get('/api/og/tokens', 'admin')],
  'api/owner-prices.ts': [post('/api/owner-prices', 'admin')],
  'api/owners.ts': [get(`/api/owners?${AT}`, 'viewer')],
  'api/path-index.ts': [get(`/api/path-index?${AT}`, 'viewer')],
  'api/plan-sweep/dispatch.ts': [post('/api/plan-sweep/dispatch', 'admin')],
  'api/plan-sweep/jobs.ts': [get('/api/plan-sweep/jobs', 'viewer')],
  'api/plan-sweep/purge.ts': [post('/api/plan-sweep/purge', 'admin')],
  'api/plan-sweep/stop.ts': [post('/api/plan-sweep/stop', 'admin')],
  'api/plan-sweep/undo.ts': [post('/api/plan-sweep/undo', 'admin')],
  'api/plans/[[path]].ts': [
    get('/api/plans', 'viewer'),
    post('/api/plans', 'admin'),
    get('/api/plans/staged', 'viewer'),
    // The scan job's ping: one grant, by id; a deployment that names none has no such route.
    post('/api/plans/staged/card', 'named-grant', w => ({ STAGED_CARD_GRANT: w.readOnlyGrant })),
    post('/api/plans/staged/card', 'unconfigured'),
    // Staging: on a deployment that stages, else no such route.
    post('/api/plans/stage', 'stager', STAGING),
    post('/api/plans/stage', 'unconfigured'),
    get('/api/plans/run?id=none', 'viewer'),
    get('/api/plans/run-analytics?id=none', 'viewer'),
    get('/api/plans/1', 'viewer'),
    patch('/api/plans/1', 'admin'),
    post('/api/plans/1/slack', 'admin'),
    post('/api/plans/1/items', 'admin'),
    post('/api/plans/1/items', 'admin', STAGING),
    // Unstaging: admins, and on a deployment that stages a stager (their own items).
    del('/api/plans/1/items', 'admin'),
    del('/api/plans/1/items', 'stager', STAGING),
    post('/api/plans/1/batches/merge', 'stager', STAGING),
    post('/api/plans/1/batches/merge', 'unconfigured'),
  ],
  'api/plans/prefix-history.ts': [get('/api/plans/prefix-history', 'viewer')],
  'api/plans/progress.ts': [get('/api/plans/progress', 'viewer')],
  'api/plans/scan-deletions.ts': [get('/api/plans/scan-deletions', 'viewer')],
  'api/prefixes.ts': [post('/api/prefixes', 'viewer')],
  'api/scan-runs/[[path]].ts': [get('/api/scan-runs', 'viewer')],
  'api/series.ts': [get('/api/series', 'viewer')],
  'api/session-log/[[path]].ts': [
    // The config says only whether logging is on (and purges only for a viewer); a batch is a viewer's, while it is on
    // (off: a 503 before the gate).
    get('/api/session-log', 'public'),
    post('/api/session-log', 'viewer'),
    get('/api/session-log/switch', 'admin'),
    put('/api/session-log/switch', 'admin'),
    get('/api/session-log/sessions', 'admin'),
    get('/api/session-log/sessions/none', 'admin'),
    get('/api/session-log/latency', 'admin'),
  ],
  'api/static-bench.ts': [get('/api/static-bench', 'viewer'), post('/api/static-bench', 'viewer')],
  'api/store.ts': [get('/api/store', 'viewer')],
  'api/subtree.ts': [get(`/api/subtree?${AT}`, 'viewer')],
  'api/sweep/dispatch.ts': [post('/api/sweep/dispatch', 'admin')],
  'api/sweep/jobs.ts': [get('/api/sweep/jobs', 'viewer')],
  'api/sweep/logs.ts': [get('/api/sweep/logs', 'viewer')],
  'api/sweep/settings.ts': [get('/api/sweep/settings', 'viewer'), put('/api/sweep/settings', 'admin')],
  'api/sweep/soft-delete.ts': [get('/api/sweep/soft-delete', 'viewer')],
  'api/sweep/stop.ts': [post('/api/sweep/stop', 'admin')],
  'api/sweep/undo.ts': [post('/api/sweep/undo', 'admin')],
  // A personal agent token is its email session's alone to manage.
  'api/token.ts': [get('/api/token', 'session'), post('/api/token', 'session'), del('/api/token', 'session')],
  'api/union-scans.ts': [get('/api/union-scans', 'viewer')],
  'api/usage.ts': [get('/api/usage', 'admin')],
  'api/whoami.ts': [get('/api/whoami', 'email')],
  // Page unfurls: a title and a description for a crawler (`/staged`: the open plan's item count), no names or sizes.
  'assignments.ts': [get('/assignments', 'public')],
  // Sign-in itself: dormant (503) until the deployment has its mail sender / its Google client.
  'auth/email/[[path]].ts': [
    post('/auth/email/start', 'public', MAIL),
    post('/auth/email/code', 'public', MAIL),
    get('/auth/email/verify', 'public', MAIL),
    get('/auth/email/poll', 'public', MAIL),
    post('/auth/email/start', 'unconfigured'),
  ],
  'auth/google.ts': [get('/auth/google', 'public', GOOGLE), get('/auth/google', 'unconfigured')],
  'auth/google/callback.ts': [get('/auth/google/callback', 'public', GOOGLE), get('/auth/google/callback', 'unconfigured')],
  'auth/google/client.ts': [get('/auth/google/client', 'public')],
  'auth/google/onetap.ts': [post('/auth/google/onetap', 'google-credential', GOOGLE), post('/auth/google/onetap', 'unconfigured')],
  'auth/google/onetap/nonce.ts': [get('/auth/google/onetap/nonce', 'public')],
  // "Sign in with OA" (`specs/oa-login-rp.md`): sign-in itself; a 404 until the deployment has its broker client.
  'auth/oa.ts': [get('/auth/oa', 'public', OA_LOGIN), get('/auth/oa', 'unconfigured')],
  'auth/oa/callback.ts': [get('/auth/oa/callback', 'public', OA_LOGIN), get('/auth/oa/callback', 'unconfigured')],
  // Whether the wall shows the button.
  'auth/oa/client.ts': [get('/auth/oa/client', 'public')],
  'data/[[path]].ts': [get('/data/scans.json', 'viewer'), get('/data/2026-10-01/meta.json', 'viewer')],
  // A card: its signature picks its tier (unsigned: the anonymous card), nothing signs in.
  'og/[[path]].ts': [get('/og/map.png', 'public')],
  'og/index.ts': [get('/og', 'public')],
  'slack/actions.ts': [post('/slack/actions', 'slack-signature', { SLACK_SIGNING_SECRET: 'slack-secret' }), post('/slack/actions', 'unconfigured')],
  'staged.ts': [get('/staged', 'public')],
  'user/[id].ts': [get('/user/ann', 'public')],
  'users.ts': [get('/users', 'public')],
  'v1/files/[[path]].ts': [get('/v1/files/list?prefix=snapshots/', 'viewer')],
  // Scanned objects: a member, never a read-only guest.
  'v1/objects/[[path]].ts': [get('/v1/objects/data-bucket/get?path=x', 'stager', { OBJECT_BUCKETS: 'data-bucket' })],
}

/** Every route of this branch: the shared ones above, and the ones only this deployment carries
 *  (`_lib/gatesDeployment.ts`, which a deployment branch fills and `cloud` leaves empty). */
const ROUTES: Record<string, Case[]> = { ...SHARED_ROUTES, ...(DEPLOYMENT_ROUTES as Record<string, Case[]>) }

// ---- a path-restricted link ----------------------------------------------------------------------------------------

/** What a path-restricted share link gets from a route its scopes reach. */
type Restricted =
  /** Refused: the 403 `FORBIDDEN_RESTRICTED`, by the middleware (`RESTRICTED_BLOCKED`) or by the route itself. */
  | 'blocked'
  /** Answered with what the link hides left out (a hidden path a 404, hidden rows dropped): the route's own tests. */
  | 'filtered'
  /** Answered as for any caller past its gate: nothing in it names a scanned path (deployment config, the caller's
   *  identity, scan ids, a rendering of the request's own body). */
  | 'pathless'
  /** A public route: the link's credential earns nothing an anonymous caller doesn't get. */
  | 'anonymous'

/** The gates a share link can pass with the scopes a restricted link is minted with: everything but `admin` (a grant
 *  holding it can mint itself an unrestricted link, so no hide scope confines it) and an email session's. `email`: a
 *  link naming an address (this audit's links name none, so the case only says it is declared). `unconfigured` answers
 *  everyone alike, before any gate; `slack-signature` and `google-credential` read no site identity. */
const LINK_GATES: ReadonlySet<Gate> = new Set<Gate>(['public', 'signed-in', 'viewer', 'stager', 'assigner', 'email', 'named-grant'])

/** A case's key in `RESTRICTED`: its method and path, without the query. */
const linkKey = (c: Case): string => `${c.method} ${c.path.split('?')[0]}`

const SHARED_RESTRICTED: Record<string, Restricted> = {
  'GET /a/0000.png': 'anonymous',
  // The ledger: D1-held prefixes, unfiltered.
  'GET /api/actions': 'blocked',
  'POST /api/actions': 'blocked',
  // A hidden path, and a hidden dir itself (its pyramid breaks its contents down), are a 404.
  'GET /api/age-pyramid': 'filtered',
  // No note pinned strictly below a hidden path.
  'GET /api/annotations': 'filtered',
  'GET /api/assignments': 'blocked',
  // The caller's own session, profile and face; the access-request flow.
  'GET /api/auth/whoami': 'pathless',
  'POST /api/auth/exchange': 'pathless',
  'POST /api/auth/logout': 'pathless',
  'POST /api/auth/track': 'pathless',
  'GET /api/auth/profile': 'pathless',
  'POST /api/auth/request': 'pathless',
  'GET /api/auth/decide': 'pathless',
  'POST /api/auth/decide': 'pathless',
  'GET /api/auth/avatar/none': 'pathless',
  'GET /api/db': 'blocked',
  'GET /api/db/allowed_emails': 'blocked',
  // A hidden path is a 404, hidden rows are dropped, a name filter is refused (its totals count hidden hits).
  'GET /api/diff': 'filtered',
  // A hidden directory, and a hidden dir itself, have no level, and `more` / `wide` name neither.
  'GET /api/diff/level': 'filtered',
  // Draws a diff of whatever path the request names.
  'POST /api/digest-image': 'blocked',
  // Renders the request's own rows; reads nothing.
  'POST /api/digest-op': 'pathless',
  'GET /api/digest-settings': 'pathless',
  // By the route: its groups name paths in every bucket.
  'GET /api/dups': 'blocked',
  'GET /api/estate': 'blocked',
  'GET /api/filter-caps': 'pathless',
  'GET /api/filter-cover': 'blocked',
  'GET /api/filter-scans': 'pathless',
  'GET /api/health': 'blocked',
  'GET /api/largest': 'filtered',
  'GET /api/me': 'pathless',
  'GET /api/name-matches': 'blocked',
  'GET /api/name-summary-registry': 'blocked',
  'POST /api/name-summary-registry': 'blocked',
  'GET /api/name-summary': 'blocked',
  'POST /api/name-summary': 'blocked',
  'POST /api/og/mint': 'blocked',
  'DELETE /api/og/tokens/none': 'blocked',
  'GET /api/owners': 'blocked',
  'GET /api/path-index': 'blocked',
  'GET /api/plan-sweep/jobs': 'blocked',
  'GET /api/plans': 'blocked',
  'GET /api/plans/staged': 'blocked',
  'POST /api/plans/staged/card': 'blocked',
  'POST /api/plans/stage': 'blocked',
  'GET /api/plans/run': 'blocked',
  'GET /api/plans/run-analytics': 'blocked',
  'GET /api/plans/1': 'blocked',
  'DELETE /api/plans/1/items': 'blocked',
  'POST /api/plans/1/batches/merge': 'blocked',
  'GET /api/plans/prefix-history': 'blocked',
  'GET /api/plans/progress': 'blocked',
  'GET /api/plans/scan-deletions': 'blocked',
  // By the route: it sizes whatever prefixes the request names.
  'POST /api/prefixes': 'blocked',
  'GET /api/scan-runs': 'blocked',
  // A hidden path is a 404; a name filter is refused (its points count hidden hits).
  'GET /api/series': 'filtered',
  // Whether logging is on; the caller's own batch.
  'GET /api/session-log': 'anonymous',
  'POST /api/session-log': 'pathless',
  'GET /api/static-bench': 'blocked',
  'POST /api/static-bench': 'blocked',
  // The scan store's bucket and its listing prefixes: config.
  'GET /api/store': 'pathless',
  // A hidden path is a 404, a hidden dir a leaf, a name filter refused (its tree counts hidden hits).
  'GET /api/subtree': 'filtered',
  'GET /api/sweep/jobs': 'blocked',
  'GET /api/sweep/logs': 'blocked',
  'GET /api/sweep/settings': 'blocked',
  'GET /api/sweep/soft-delete': 'blocked',
  'GET /api/union-scans': 'pathless',
  'GET /api/whoami': 'pathless',
  'GET /assignments': 'anonymous',
  'POST /auth/email/start': 'anonymous',
  'POST /auth/email/code': 'anonymous',
  'GET /auth/email/verify': 'anonymous',
  'GET /auth/email/poll': 'anonymous',
  'GET /auth/google': 'anonymous',
  'GET /auth/google/callback': 'anonymous',
  'GET /auth/google/client': 'anonymous',
  'GET /auth/google/onetap/nonce': 'anonymous',
  'GET /auth/oa': 'anonymous',
  'GET /auth/oa/callback': 'anonymous',
  'GET /auth/oa/client': 'anonymous',
  // The scan list, and a scan's `meta.json`; of the rest, `age.json` unless a hidden dir is top-level, and
  // `rules.json` without the rules below a hidden path (`_lib/restrict.ts` `dataAllowed`, `restrictRules`).
  'GET /data/scans.json': 'filtered',
  'GET /data/2026-10-01/meta.json': 'filtered',
  // A restricted link's `key=` never earns the full card (`og/tokens.ts`).
  'GET /og/map.png': 'anonymous',
  'GET /og': 'anonymous',
  'GET /staged': 'anonymous',
  'GET /user/ann': 'anonymous',
  'GET /users': 'anonymous',
  'GET /v1/files/list': 'blocked',
  'GET /v1/objects/data-bucket/get': 'blocked',
}

/** Every declared answer: the shared routes', and this deployment's own (`_lib/gatesDeployment.ts`). */
const RESTRICTED: Record<string, Restricted> = { ...SHARED_RESTRICTED, ...(DEPLOYMENT_RESTRICTED as Record<string, Restricted>) }

/** What a path-restricted link is refused with (`_lib/restrict.ts` `restrictedForbidden`). */
const FORBIDDEN_RESTRICTED = '{"error":"forbidden for a path-restricted link"}\n'
/** The directory the restricted link hides. */
const HIDDEN = 'bk/secret'

// ---- the route modules ---------------------------------------------------------------------------------------------

type Handler = (ctx: Record<string, unknown>) => Response | Promise<Response>
type RouteModule = Record<string, unknown>
type Glob = (patterns: string[]) => Record<string, () => Promise<RouteModule>>

/** Every route module: all of `functions/` but the library, the tests and the middleware. */
const LOADERS = (import.meta as ImportMeta & { glob: Glob }).glob(['./**/*.ts', '!./_lib/**', '!./**/*.test.ts', '!./_middleware.ts'])
const FILES = Object.keys(LOADERS).map(k => k.replace(/^\.\//, '')).sort()

const handlerNames = (mod: RouteModule): string[] => Object.keys(mod).filter(k => k.startsWith('onRequest')).sort()
/** The export Pages calls for `method`: its own (`onRequestGet`), else the all-method `onRequest`. */
const handlerFor = (mod: RouteModule, method: string): string => {
  const own = `onRequest${method[0]}${method.slice(1).toLowerCase()}`
  return own in mod ? own : 'onRequest'
}

/** A route file's path pattern, by segment (`og/index.ts` is `/og`). */
const patternOf = (file: string): string[] => {
  const segs = file.replace(/\.ts$/, '').split('/')
  return segs.at(-1) === 'index' ? segs.slice(0, -1) : segs
}

/** Pages' `params` for `path` on `file`'s route (`[id]`: a segment; `[[path]]`: the rest, as an array), or null
 *  when the file's route doesn't match the path. */
function paramsOf(file: string, path: string): Record<string, string | string[]> | null {
  const pattern = patternOf(file)
  const segs = path.split('?')[0].split('/').filter(Boolean)
  const params: Record<string, string | string[]> = {}
  for (const [i, p] of pattern.entries()) {
    const rest = /^\[\[(\w+)\]\]$/.exec(p)
    if (rest) return { ...params, [rest[1]]: segs.slice(i) }
    if (i >= segs.length) return null
    const one = /^\[(\w+)\]$/.exec(p)
    if (one) params[one[1]] = segs[i]
    else if (p !== segs[i]) return null
  }
  return segs.length === pattern.length ? params : null
}

/** A route's rank among those matching one path, as Pages picks: per segment, a literal over `[one]` over
 *  `[[rest]]`; an exact end over a catch-all's empty rest. */
const rankOf = (file: string): string => [...patternOf(file).map(p => (p.startsWith('[[') ? 0 : p.startsWith('[') ? 1 : 2)), 3].join('')
/** The route file that serves `path`. */
const servedBy = (path: string): string | null => FILES.filter(f => paramsOf(f, path)).sort((a, b) => (rankOf(a) < rankOf(b) ? 1 : -1))[0] ?? null

// ---- the callers ---------------------------------------------------------------------------------------------------

// Not localhost: a localhost request is the full-scope dev identity (`identify`).
const ORIGIN = 'https://gcs.example.test'
const STAFF_DOMAIN = 'staff.example.test'
const EMAILS = { viewer: 'vera@example.org', staff: `sam@${STAFF_DOMAIN}`, listed: 'lee@example.org' }

interface Mode {
  name: string
  listOn: boolean
  vars: Record<string, string>
}
const MODES: Mode[] = [
  { name: '`ADMIN_LIST` on: admin is the list, the staff domain alone is not', listOn: true, vars: { ADMIN_LIST: '1' } },
  // `ADMIN_EMAILS`: the `admin_emails` table is read, so the listed address stays an admin beside staff.
  { name: '`ADMIN_LIST` off: staff are admins', listOn: false, vars: { ADMIN_EMAILS: '1' } },
]

async function world(mode: Mode): Promise<World> {
  const { db, raw } = await sqliteD1()
  withAdminEmails(raw)
  withLedger(raw)
  const env = {
    BASE_SCOPE: 'gcs', SESSION_SECRET: 'gates-test-secret', STAFF_DOMAIN, DB: db,
    // The index store "configured": the scan-reading routes check it before their gate.
    GCS_HMAC_KEY_ID: 'key', GCS_HMAC_SECRET: 'secret', STORE_BUCKET: 'index-bucket',
    // No SPA shell: a page unfurl passes the asset server's answer on.
    ASSETS: { fetch: async () => new Response('no static assets in the gate audit', { status: 404 }) },
    ...mode.vars,
  } as unknown as Env
  const row = 'VALUES (?, NULL, ?, 0)'
  await db.prepare(`INSERT INTO allowed_emails (email, note, who, ts) ${row}`).bind(EMAILS.viewer, 'test').run()
  await db.prepare(`INSERT INTO admin_emails (email, note, who, ts) ${row}`).bind(EMAILS.listed, 'test').run()
  // The list admits whom it names; without it, an `admin_emails` row signs in only where something else admits it.
  if (!mode.listOn) await db.prepare(`INSERT INTO allowed_emails (email, note, who, ts) ${row}`).bind(EMAILS.listed, 'test').run()
  // Session logging on (no end date), so a batch reaches its gate.
  await db.prepare('INSERT INTO session_log_switch (id, until_ms, who, ts) VALUES (1, ?, ?, 0)').bind(8_640_000_000_000_000, 'test').run()

  const gate = gateFor(env)!
  const session = async (email: string): Promise<Record<string, string>> => {
    const signed = await gate.signIn(email, new Request(`${ORIGIN}/signin`))
    if (!signed) throw new Error(`${email} is not admitted to sign in (${mode.name})`)
    return { cookie: signed.cookie.split(';')[0] }
  }
  // Minted with the list off, on the same database: the list refuses to mint an admin grant, and one from before it stands.
  const before = gateFor({ ...env, ADMIN_LIST: undefined })!
  const grant = async (name: string, scopes: string[], email: string | null) => {
    const { grant, token } = await before.mint({ email, scopes, name, createdBy: 'test', expiresAt: null, maxRedeems: null })
    return { id: grant.id, headers: { authorization: `Bearer ${token}` } }
  }
  const readOnly = await grant('read-only link', ['gcs:read'], null)
  return {
    env,
    readOnlyGrant: readOnly.id,
    links: {
      restricted: (await grant('restricted link', ['gcs', 'gcs:assign', hideScope('gcs', HIDDEN)], null)).headers,
      open: (await grant('open link', ['gcs', 'gcs:assign'], null)).headers,
    },
    headers: {
      'anonymous': {},
      'read-only link': readOnly.headers,
      'viewer': await session(EMAILS.viewer),
      'staff, not listed': await session(EMAILS.staff),
      'listed admin': await session(EMAILS.listed),
      'admin grant': (await grant('admin link', ['gcs', 'admin'], null)).headers,
      // What `/api/token` mints for the listed admin: their email, the base scope and assign, never admin.
      'agent token': (await grant('CLI / agent token', ['gcs', 'gcs:assign'], EMAILS.listed)).headers,
    },
  }
}

/** A hung handler is a failure naming the route, never a pass. */
const CALL_TIMEOUT_MS = 5_000
const HUNG = Symbol('hung')

/** One call, as `headers`' caller: what `run` did with the case's context (its status, or its throw), and a 403's
 *  body. `as` names the caller in a hang's message. */
async function answer(w: World, run: Handler, file: string, c: Case, headers: Record<string, string>, as: string): Promise<{ did: string; body?: string }> {
  const vars = typeof c.env === 'function' ? c.env(w) : c.env
  const bodied = c.method !== 'GET'
  const request = new Request(`${ORIGIN}${c.path}`, {
    method: c.method,
    headers: { ...headers, ...(bodied ? { 'content-type': 'application/json' } : {}) },
    ...(bodied ? { body: '{}' } : {}),
  })
  const ctx = {
    request,
    env: { ...w.env, ...vars },
    params: paramsOf(file, c.path),
    data: {},
    // Work a handler leaves behind may fail on the bindings this env lacks.
    waitUntil: (p: Promise<unknown>) => { p.catch(() => undefined) },
    next: async () => new Response('next', { status: 404 }),
  }
  let timer: ReturnType<typeof setTimeout> | null = null
  const hung = new Promise<typeof HUNG>(resolve => { timer = setTimeout(() => resolve(HUNG), CALL_TIMEOUT_MS) })
  const answered = (async () => run(ctx))().then(
    async r => ({ did: String(r.status), ...(r.status === 403 ? { body: await r.text() } : {}) }),
    e => ({ did: `threw ${String((e as Error)?.message ?? e).slice(0, 80)}` }),
  )
  const got = await Promise.race([answered, hung])
  clearTimeout(timer)
  if (got === HUNG) throw new Error(`${c.method} ${c.path} (${file}) as ${as}: no answer in ${CALL_TIMEOUT_MS} ms`)
  return got
}

/** One call's outcome, and what the handler did (its status, or its throw) for a failure's message. */
async function call(w: World, handler: Handler, file: string, c: Case, who: Who): Promise<{ outcome: Outcome; did: string }> {
  const { did } = await answer(w, handler, file, c, w.headers[who], who)
  return { outcome: did === '401' ? 401 : did === '403' ? 403 : 'pass', did }
}

/** A route as a deployed request reaches it: the middleware's guard for a path-restricted link (`_middleware.ts`
 *  `restrictGate`), then the handler. */
const behindMiddleware = (handler: Handler): Handler => async ctx =>
  (await restrictGate(ctx.request as Request, ctx.env as Env)) ?? handler(ctx)

/** `POST /api/auth/grants` as `who`, through the route. */
const mintAs = (w: World, who: Who, scopes: string[]): Promise<Response> => (MODULES['api/auth/[[path]].ts'].onRequest as Handler)({
  request: new Request(`${ORIGIN}/api/auth/grants`, { method: 'POST', headers: { ...w.headers[who], 'content-type': 'application/json' }, body: JSON.stringify({ name: 'a link', scopes }) }),
  env: w.env,
}) as Promise<Response>

const expected = (gate: Gate, mode: Mode): Outcome[] => EXPECT[gate].map(cell => (typeof cell === 'object' ? (mode.listOn ? cell.listOn : cell.listOff) : cell))
const byIdentity = <T>(vs: T[]): Record<string, T> => Object.fromEntries(IDENTITIES.map((who, i) => [who, vs[i]]))

// ---- the audit -----------------------------------------------------------------------------------------------------

const MODULES: Record<string, RouteModule> = {}

beforeAll(async () => {
  // Nothing leaves the process. A 404, not a 5xx: the stores' S3 client (`aws4fetch`) retries a 5xx with backoff,
  // long past the timeout.
  vi.stubGlobal('fetch', async () => new Response('no network in the gate audit', { status: 404 }))
  // The colo cache: always a miss.
  vi.stubGlobal('caches', { default: { match: async () => undefined, put: async () => undefined, delete: async () => false } })
  for (const file of FILES) MODULES[file] = await LOADERS[`./${file}`]()
})
afterAll(() => { vi.unstubAllGlobals() })

describe('every route declares its gate', () => {
  it('the route files are exactly the ones `ROUTES` declares', () => {
    expect(FILES).toEqual(Object.keys(ROUTES).sort())
  })
  it('each file exports exactly the handlers its cases reach', () => {
    const exported = Object.fromEntries(FILES.map(f => [f, handlerNames(MODULES[f])]))
    const reached = Object.fromEntries(FILES.map(f => [f, [...new Set((ROUTES[f] ?? []).map(c => handlerFor(MODULES[f], c.method)))].sort()]))
    expect(reached).toEqual(exported)
  })
  it('each case\'s path is served by the file it is declared under (the most specific route wins)', () => {
    const elsewhere = Object.entries(ROUTES).flatMap(([file, cases]) => cases
      .filter(c => servedBy(c.path) !== file)
      .map(c => `${c.method} ${c.path}: declared under ${file}, served by ${servedBy(c.path)}`))
    expect(elsewhere).toEqual([])
  })
  it('every gate in the vocabulary is some route\'s', () => {
    expect([...new Set(Object.values(ROUTES).flatMap(cases => cases.map(c => c.gate)))].sort()).toEqual(Object.keys(EXPECT).sort())
  })
  it('the cases a share link\'s scopes reach are exactly the ones `RESTRICTED` answers for a path-restricted link', () => {
    const reachable = [...new Set(Object.values(ROUTES).flatMap(cases => cases.filter(c => LINK_GATES.has(c.gate)).map(linkKey)))].sort()
    expect(Object.keys(RESTRICTED).sort()).toEqual(reachable)
  })
})

describe.each(MODES)('$name', mode => {
  let w: World
  beforeAll(async () => { w = await world(mode) })

  it('the seven identities are who they say (how they signed in, scopes, admin)', async () => {
    const ids = await Promise.all(IDENTITIES.map(async who => {
      const id = await identify({ request: new Request(`${ORIGIN}/api/me`, { headers: w.headers[who] }), env: w.env })
      return id && { via: id.via, email: id.email, scopes: id.scopes, admin: id.admin }
    }))
    const staffScopes = mode.listOn ? ['gcs', 'cw', 'gcs:assign'] : ['gcs', 'cw', 'admin', 'gcs:assign']
    expect(byIdentity(ids)).toEqual({
      'anonymous': null,
      'read-only link': { via: 'grant', email: null, scopes: ['gcs:read'], admin: false },
      'viewer': { via: 'session', email: EMAILS.viewer, scopes: ['gcs', 'gcs:assign'], admin: false },
      'staff, not listed': { via: 'session', email: EMAILS.staff, scopes: staffScopes, admin: !mode.listOn },
      'listed admin': { via: 'session', email: EMAILS.listed, scopes: ['gcs', 'gcs:assign', 'admin'], admin: true },
      'admin grant': { via: 'grant', email: null, scopes: ['gcs', 'admin'], admin: true },
      'agent token': { via: 'grant', email: EMAILS.listed, scopes: ['gcs', 'gcs:assign'], admin: false },
    })
  })

  // What `admin` lets a caller mint: under the list, no grant carrying `admin` or `*`, for any admin.
  it.each([
    { who: 'listed admin', scopes: ['admin'] },
    { who: 'listed admin', scopes: ['*'] },
    { who: 'admin grant', scopes: ['gcs', 'admin'] },
    { who: 'admin grant', scopes: ['*'] },
  ] as { who: Who; scopes: string[] }[])('$who minting a grant with $scopes', async ({ who, scopes }) => {
    const res = await mintAs(w, who, scopes)
    expect(mode.listOn ? [res.status, await res.json()] : res.status).toEqual(mode.listOn ? [403, { error: ADMIN_GRANT_REFUSED }] : 200)
  })
  it.each(['listed admin', 'admin grant'] as Who[])('%s minting a viewer grant', async who => {
    expect((await mintAs(w, who, ['gcs'])).status).toBe(200)
  })

  const cases = Object.entries(ROUTES).flatMap(([file, cs]) => cs.map(c => ({ file, c, title: `${c.method} ${c.path}${c.env ? ' (+env)' : ''}: ${c.gate}` })))
  it.each(cases)('$title', async ({ file, c }) => {
    const mod = MODULES[file]
    const handler = mod[handlerFor(mod, c.method)] as Handler
    const got: { outcome: Outcome; did: string }[] = []
    for (const who of IDENTITIES) got.push(await call(w, handler, file, c, who))
    const did = IDENTITIES.map((who, i) => `${who}: ${got[i].did}`).join('; ')
    expect(byIdentity(got.map(g => g.outcome)), `${c.method} ${c.path} (${file}) answered ${did}`).toEqual(byIdentity(expected(c.gate, mode)))
  })

  it('the two share links are who they say: the same scopes, one hiding a directory', async () => {
    const ids = await Promise.all([w.links.restricted, w.links.open].map(async headers => {
      const id = await identify({ request: new Request(`${ORIGIN}/api/me`, { headers }), env: w.env })
      return id && { via: id.via, email: id.email, scopes: id.scopes, admin: id.admin }
    }))
    expect(ids).toEqual([
      { via: 'grant', email: null, scopes: ['gcs', 'gcs:assign', `gcs:hide:${HIDDEN}`], admin: false },
      { via: 'grant', email: null, scopes: ['gcs', 'gcs:assign'], admin: false },
    ])
  })

  const linkCases = cases.filter(({ c }) => LINK_GATES.has(c.gate)).map(x => ({ ...x, title: `a path-restricted link, ${x.c.method} ${x.c.path}${x.c.env ? ' (+env)' : ''}: ${RESTRICTED[linkKey(x.c)] ?? 'undeclared'}` }))
  it.each(linkCases)('$title', async ({ file, c }) => {
    const mod = MODULES[file]
    const deployed = behindMiddleware(mod[handlerFor(mod, c.method)] as Handler)
    const restricted = await answer(w, deployed, file, c, w.links.restricted, 'the restricted link')
    const open = await answer(w, deployed, file, c, w.links.open, 'the open link')
    // Refused as a restricted link is, or answered exactly as the same link without its restriction.
    const got = restricted.did === '403' && restricted.body === FORBIDDEN_RESTRICTED ? 'blocked'
      : restricted.did === open.did ? 'as the open link'
      : `${restricted.did}, the open link ${open.did}`
    const declared = RESTRICTED[linkKey(c)]
    expect(got, `${c.method} ${c.path} (${file})`).toEqual(declared === undefined ? 'undeclared' : declared === 'blocked' ? 'blocked' : 'as the open link')
  })
})
