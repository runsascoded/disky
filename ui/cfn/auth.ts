/**
 * The gate over the static deployment (spec `specs/done/pages-auth.md`): a laptop
 * listing is private, so every `/api/*` route needs a `view` session — an SSO
 * identity on the allowlist, or a share link (`?key=`) minted by an admin.
 *
 * Tier 2 of `@open-athena/auth`, hosted right here in the Pages Functions:
 * HMAC session cookies + D1-backed grants, CF Access reduced to an SSO IdP on
 * `/auth/sso`. `buildGate` takes the stores explicitly so the tests run the
 * same code over the package's in-memory stores; `gateFor` is the D1 wiring.
 */
import {
  type AuditQuery,
  type AuditSink,
  type Auth,
  type EmailPolicy,
  type Gate,
  type GrantStore,
  authRoutes,
  createGate,
  hasScope,
} from '@open-athena/auth'
import { d1AuditQuery, d1AuditSink, d1GrantStore } from '@open-athena/auth/d1'
import type { Env } from './env'

export const COOKIE = 'disk_tree_auth'
/** Read scans. Every share link gets this. */
export const VIEW_SCOPE = 'view'
/** Mint / revoke links, read the access log. Allowlisted SSO identities only. */
export const ADMIN_SCOPE = 'admin'

/** `/api/*` paths that stay public: the UI needs them to decide whether to show the wall. */
export const PUBLIC_API = new Set(['/api/capabilities'])

/** Exact-email allowlist (case-insensitive); everyone else is refused at SSO. */
export function allowlistPolicy(emails: string | undefined): EmailPolicy {
  const set = new Set(
    (emails ?? '')
      .split(/[\s,]+/)
      .filter(Boolean)
      .map(e => e.toLowerCase()),
  )
  return email => (set.has(email.toLowerCase()) ? [VIEW_SCOPE, ADMIN_SCOPE] : null)
}

export interface GateDeps {
  store: GrantStore
  audit: AuditSink
  secret: string
  policy: EmailPolicy
}

export const buildGate = ({ store, audit, secret, policy }: GateDeps): Gate =>
  createGate({
    store,
    audit,
    secret,
    policy,
    cookieName: COOKIE,
    // On: the point of a named link is seeing who opened it and what they
    // read. Disclosed on the wall and in the header chip.
    logViews: true,
  })

/**
 * A fixed dev secret when the request is to localhost and no real secret is
 * configured — `pnpm cfn:dev` works with zero setup, while a deployment
 * without `SESSION_SECRET` fails loudly rather than signing with a known key.
 */
function secretFor(env: Env, req: Request): string {
  if (env.SESSION_SECRET) return env.SESSION_SECRET
  const { hostname } = new URL(req.url)
  if (hostname === 'localhost' || hostname === '127.0.0.1') return 'dev-only-insecure-secret-do-not-ship'
  throw new Error('SESSION_SECRET is not configured')
}

export function gateFor(env: Env, req: Request): { gate: Gate; audit: AuditQuery } {
  if (!env.DB) throw new Error('DB (D1) binding is not configured')
  const gate = buildGate({
    store: d1GrantStore(env.DB),
    audit: d1AuditSink(env.DB),
    secret: secretFor(env, req),
    policy: allowlistPolicy(env.ALLOWED_EMAILS),
  })
  return { gate, audit: d1AuditQuery(env.DB) }
}

/** The authenticated identity behind a request (`null` if none) — for a
 *  Function that needs to know *who* is acting, or check a scope beyond the
 *  `view` the middleware already enforced. */
export const authFor = (env: Env, req: Request): Promise<Auth | null> => gateFor(env, req).gate.authenticate(req)

/** A stable, human label for an `Auth`, recorded as an action's actor. */
export const actorLabel = (auth: Auth): string =>
  auth.kind === 'sso'
    ? auth.email
    : auth.grant.name ?? auth.grant.email ?? auth.grant.subject?.email ?? `grant:${auth.grant.id}`

const json = (data: unknown, status: number): Response =>
  new Response(JSON.stringify(data) + '\n', {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  })

/**
 * The middleware core. Non-API paths and `PUBLIC_API` pass through;
 * `/api/auth/*` is the package's surface (whoami, exchange, logout, grants,
 * log — admin routes check `ADMIN_SCOPE` themselves); everything else under
 * `/api/` needs an authenticated `view` session.
 */
export async function gateApi(
  gate: Gate,
  request: Request,
  next: () => Promise<Response>,
  audit?: AuditQuery,
): Promise<Response> {
  const { pathname } = new URL(request.url)
  if (!pathname.startsWith('/api/') || PUBLIC_API.has(pathname)) return next()
  const routed = await authRoutes(gate, { audit, adminScope: ADMIN_SCOPE })(request)
  if (routed) return routed
  const auth = await gate.authenticate(request)
  if (!auth) return json({ error: 'unauthenticated' }, 401)
  if (!hasScope(auth, VIEW_SCOPE)) return json({ error: 'forbidden' }, 403)
  const res = await next()
  // Private data: nothing between the worker and the browser may keep a copy.
  const out = new Response(res.body, res)
  out.headers.set('cache-control', 'no-store')
  return out
}
