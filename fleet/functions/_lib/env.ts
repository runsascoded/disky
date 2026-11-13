/**
 * The fleet app's env and its one gate (specs/disky-oa-dev.md): an `@open-athena/auth` gate with no storage. A person
 * signs in through OA's broker (`/auth/oa`, `@disk-tree/pages-kit/oa`) and gets a signed session cookie under
 * `SESSION_SECRET`; the policy admits `STAFF_DOMAIN` addresses. There are no share links or tokens to look up, so the
 * gate's grant store is empty (`NO_GRANTS`): a session is judged by its signature alone, and the app needs no D1.
 */
import { createGate, domainPolicy, type Gate, type GrantStore, readCookie, DEFAULT_COOKIE_NAME } from '@open-athena/auth'
import { oaLoginOptions, type OaLoginCfg } from '@disk-tree/pages-kit/oa'

export interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> }
  /** Signs sessions and card URLs (`ogKey`). */
  SESSION_SECRET?: string
  /** Who may sign in: addresses at these domains (comma-separated). */
  STAFF_DOMAIN?: string
  OA_LOGIN_ISSUER?: string
  OA_LOGIN_CLIENT_ID?: string
  OA_LOGIN_CLIENT_SECRET?: string
  /** The deployments, a JSON list (`fleet.ts` `parseFleet`); each names the secret holding its read token. */
  FLEET_JSON?: string
  /** How many runs per deployment `/api/fleet` returns (default `DEFAULT_RUNS`). */
  FLEET_RUNS?: string
  /** The Bearer token `POST /api/fleet/slack` takes (the hourly cron's). */
  FLEET_JOB_TOKEN?: string
  /** "Disky Admin Bot"'s token, and the channel (an id) its fleet post lives in. */
  DISKY_SLACK_BOT_TOKEN?: string
  SLACK_FLEET_CHANNEL?: string
  /** The post's ts, when it's pinned by hand rather than kept in `FLEET_KV`. */
  SLACK_FLEET_TS?: string
  /** Where the post's channel + ts are kept between runs. */
  FLEET_KV?: KVNamespace
  /** Local dev only (`.dev.vars`, never a Pages secret): a localhost request is this signed-in address. */
  DEV_EMAIL?: string
  /** The deployments' tokens, under the names `FLEET_JSON` gives. */
  [secret: string]: unknown
}

export interface Ctx {
  request: Request
  env: Env
  waitUntil?: (p: Promise<unknown>) => void
}

/** The session cookie (the package's default name). */
export const SESSION_COOKIE = DEFAULT_COOKIE_NAME

/** The scope a staff session carries. */
export const FLEET_SCOPE = 'fleet'

const refuse = (): never => { throw new Error('the fleet gate holds no grants') }

/** A grant store with nothing in it: every lookup misses (a presented token is refused), every write is refused. */
export const NO_GRANTS: GrantStore = {
  byId: async () => null,
  byTokenHash: async () => null,
  insert: async () => refuse(),
  redeem: async () => null,
  touch: async () => {},
  revoke: async () => false,
  setDisabled: async () => false,
  update: async () => null,
  rotate: async () => null,
  list: async () => [],
}

const domains = (env: Env): string[] => (env.STAFF_DOMAIN ?? '').split(',').map(d => d.trim().toLowerCase()).filter(Boolean)

/** The gate, or null where it isn't configured (no `SESSION_SECRET` or no `STAFF_DOMAIN`). */
export function gateFor(env: Env): Gate | null {
  const ds = domains(env)
  if (!env.SESSION_SECRET || !ds.length) return null
  return createGate({ store: NO_GRANTS, secret: env.SESSION_SECRET, policy: domainPolicy(ds, [FLEET_SCOPE]), cookieName: SESSION_COOKIE })
}

/** "Sign in with OA"'s config, or null where it isn't configured. */
export const oaConfig = (env: Env, request: Request): OaLoginCfg | null => oaLoginOptions(gateFor(env), env, request)

const isLocal = (request: Request): boolean => ['localhost', '127.0.0.1'].includes(new URL(request.url).hostname)

/** The signed-in staff address, or null. A localhost request is `DEV_EMAIL` when that's set (local dev). */
export async function identify(ctx: Ctx): Promise<string | null> {
  if (ctx.env.DEV_EMAIL && isLocal(ctx.request)) return ctx.env.DEV_EMAIL
  const gate = gateFor(ctx.env)
  // Only a session: the gate would look a Bearer token up in its (empty) store, so one is refused without asking.
  if (!gate || !readCookie(ctx.request, SESSION_COOKIE)) return null
  const auth = await gate.authenticate(ctx.request)
  return auth?.kind === 'sso' ? auth.email : null
}

export const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data) + '\n', { status, headers: { 'content-type': 'application/json; charset=utf-8', ...headers } })

/** Gate a route on a staff session: the address, or a 401. */
export async function requireStaff(ctx: Ctx): Promise<string | Response> {
  const email = await identify(ctx)
  return email ?? json({ error: 'unauthenticated' }, 401, { 'cache-control': 'no-store' })
}

/** Constant-time equality of two strings. */
export function same(a: string, b: string): boolean {
  if (a.length !== b.length) return false
  let d = 0
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i)
  return d === 0
}
