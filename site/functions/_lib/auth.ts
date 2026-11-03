/**
 * One gate for the whole site (`@open-athena/auth`).
 *
 * The identity is the app session cookie / `Authorization: Bearer` / `?key=`
 * — the `@open-athena/auth` gate, backed by D1. Sessions are minted by the
 * deployment's own Google OIDC client (`/auth/google`, or its in-page button
 * at `/auth/google/onetap`) or an emailed code (`/auth/email/*`); named share
 * links ("anyone with the link can view") redeem for a session that re-joins
 * its grant row every request, so revocation is instant. (The Cloudflare
 * Access edge JWT was a second source until every deployment cut over to
 * this model — gcs.oa.dev 2026-09-24, cw-s3.oa.dev 2026-09-28.)
 *
 * Scopes: staff (`STAFF_DOMAIN`) get everything; a viewer domain
 * (`VIEWER_DOMAINS`, e.g. coreweave.com on cw-s3) or a D1 `allowed_emails`
 * row gets the deployment's base scope — email sessions re-derive that from
 * `scopesFor` on every request, so removal bites instantly. Grant sessions
 * carry the scopes they were minted with (normally just the base scope).
 *
 * Admin is the `admin` scope, and one function says which email holds it:
 * `isAdmin`. With `ADMIN_LIST` it is the deployment's explicit list (an
 * `admin_emails` row, or the break-glass `ADMIN_BOOTSTRAP`) and the staff
 * domain alone is not; without it, staff are admins. The only other admin is
 * a grant minted with the scope (by an admin: `POST /api/auth/grants`), which
 * `ADMIN_LIST` refuses to mint (`grantStore`); one already live keeps working.
 */
import { type Auth, createGate, DEFAULT_COOKIE_NAME, type Gate, type Grant, type GrantPatch, type GrantStore, hasScope, parseScopes, readCookie, type Subject, verifySessionClaims } from '@open-athena/auth'
import { d1AuditSink, d1GrantStore, d1ProfileStore, d1RequestStore } from '@open-athena/auth/d1'
import type { AnalyticsEngineDataset, D1Database, KVNamespace, R2Bucket } from '@cloudflare/workers-types'
import { grantCaller, meterOf } from './usage.js'

export interface Env {
  DB?: D1Database
  SESSION_SECRET?: string
  /** OIDC (our own Google client) — the ZT-free sign-in path. Set as Pages
   *  secrets; see specs/done/oidc-cutover-cw.md. Absent → `/auth/google` 503s. */
  GOOGLE_CLIENT_ID?: string
  /** "Sign in with OA" (specs/oa-login-rp.md): OA's broker as an OIDC provider (`https://login.oa.dev`), this
   *  deployment's client there, and its secret. Any unset: no `/auth/oa`, no button. */
  OA_LOGIN_ISSUER?: string
  OA_LOGIN_CLIENT_ID?: string
  OA_LOGIN_CLIENT_SECRET?: string
  GOOGLE_CLIENT_SECRET?: string
  /** Email-code fallback (ZT One-Time-PIN replacement) — Resend sender + `from`
   *  address (`Name <addr@verified-domain>`). Absent → `/auth/email/*` 503s. */
  RESEND_API_KEY?: string
  MAIL_FROM?: string
  STAFF_DOMAIN?: string
  /** Comma-separated email domains admitted to the base scope with no
   *  `allowed_emails` row — what the Access policy's email-domain include did
   *  (cw-s3: `coreweave.com`). Unset = allowlist only. */
  VIEWER_DOMAINS?: string
  /** Set where the `admin_emails` table exists (cw-s3; `site/schema.sql`):
   *  its rows get `admin` on top of the base scope, beside the staff domain. Unset = staff only.
   *  (`ADMIN_LIST` reads the table whether or not this is set.) */
  ADMIN_EMAILS?: string
  /** `1`: admin is the deployment's explicit list and nothing else: an `admin_emails` row (edited on `/admin`,
   *  `/api/admins`) or the break-glass `ADMIN_BOOTSTRAP`. Staff keep every other scope; the domain alone is not
   *  admin. A listed address is admitted to sign in by the list itself. Unset: staff are admins (`isAdmin`). */
  ADMIN_LIST?: string
  /** With `ADMIN_LIST`: comma-separated emails that are admins whatever the list holds (break-glass: an empty or
   *  missing table, or a list nobody on it can use). Named addresses, never a domain; shown on `/admin` as their own
   *  path, not removable there. Set it as a secret, so the address stays out of the repo. Without `ADMIN_LIST`, unused. */
  ADMIN_BOOTSTRAP?: string
  /** Local dev only (`.dev.vars`): email the localhost dev identity acts as.
   *  Matters when the D1 binding is remote (writes land in the real ledger). */
  DEV_EMAIL?: string
  /** GCS deploys: HMAC creds for the index/data store. Optional now that the
   *  store seam is env-generalized — an r2/s3 deploy sets `STORE_*` instead
   *  (`_lib/index.ts:storeCreds`). */
  GCS_HMAC_KEY_ID?: string
  GCS_HMAC_SECRET?: string
  /** Generalized index-store seam (specs/done/union-of-roots.md — the r2/public
   *  layer). Point the index reader at any S3-compatible store; unset falls
   *  back to the GCS defaults + `GCS_HMAC_*` so gcs/cw are unchanged. */
  STORE_ENDPOINT?: string
  STORE_BUCKET?: string
  STORE_REGION?: string
  /** Comma-separated allowed key prefixes; unset = the GCS default set. */
  STORE_PREFIXES?: string
  /** Comma-separated scanned buckets whose objects `/v1/objects` serves to
   *  members (read with the store creds); unset = none. */
  OBJECT_BUCKETS?: string
  /** The plans prefix convention (`_lib/plans.ts`): the URI scheme
   *  stored prefixes carry (`s3://`, `gs://`) and the comma-separated buckets
   *  the scan covers, first = primary. Unset = the CoreWeave deployment's. */
  STORE_SCHEME?: string
  STORE_BUCKETS?: string
  /** Set where viewers stage deletions into a shared open plan
   *  (`POST /api/plans/stage`, `stage_batches` in this D1); mirrors the
   *  client's `Store.staging`. Unset = admins curate plans directly. */
  STAGING?: string
  /** `1`: the scan's duplicate-candidate report (`/api/dups`, the `/dups` page; specs/dups.md), read from the
   *  `dups.{json,parquet}` the `dups` scan step writes beside each scan's index. Unset = 404. */
  DUPS?: string
  /** The path filter's default syntax (`q=` without `qs=`; `querySyntax.ts`
   *  ids). Mirrors the client's `Store.querySyntax`. Unset = `simple`. */
  QUERY_SYNTAX?: string
  /** `1`: a single-literal `q=` is answered from the static name index on `INDEX_R2`
   *  (`_lib/staticFilter.ts`) on every scan of its generation. */
  FILTER_STATIC?: string
  /** `1`: the map's filter accepts only what the static name index answers exactly — one literal substring
   *  of a name, unscoped, on a scan the index covers (`_lib/indexedOnly.ts`); anything else is a 400 with a
   *  reason code, never a path-store scan. */
  FILTER_INDEXED_ONLY?: string
  /** The scan a view with no `?d=` opens on (`src/defaultScan.ts`): `newest` (unset), the newest published scan, its
   *  search box disabled until the static name index appends it; `searchable`, the newest scan that index covers. */
  DEFAULT_SCAN?: string
  /** `1`: the search box answers path terms (`a/b`) from the static name index (`_lib/indexedOnly.ts` `pathTerms`);
   *  unset, a term holding `/` is `unsupported-slash` on an indexed-only deployment and walked elsewhere. */
  SEARCH_PATH_TERMS?: string
  /** With `FILTER_STATIC`: how long (ms) a filtered view waits past its phase-2 read for the static
   *  roots' own rows (kind, ages, classes) before answering without them; unset = until they land. */
  FILTER_DETAILS_MS?: string
  /** A filter view's phase-2 time budget, ms (`view.ts` `FILTER_PHASE2_MS`). */
  FILTER_PHASE2_MS?: string
  /** A map card's wall-clock budget (ms) for reading its view deeper than the floor (`og/data.ts`
   *  `OG_DEPTH_BUDGET_MS`; `0`: the floor alone). */
  OG_DEPTH_BUDGET_MS?: string
  /** The deepest view a map card reads (`og/data.ts` `OG_MAX_DEPTH`). */
  OG_MAX_DEPTH?: string
  /** Worker-served indexes (the static name index's suffix shards and catalog). */
  INDEX_R2?: R2Bucket
  /** `1`: the small served files — snapshot JSONs (`snapshots/…`: `meta.json`, `lifecycle.json`,
   *  `age.json`, `rules.json`) and the extras sidecars (`<gen dir>/attr.tsv{,.idx.json}`) — are read
   *  from `INDEX_R2` under their store keys (`index.ts` `servedStore`; the job's `dt-cloud publish-r2 -S`
   *  copies them there); unset = through the store seam (`STORE_*`). */
  SERVED_R2?: string
  /** `1`: the scans' generations carry the filter's search sidecars (`path-index -S`): a filter the
   *  static index doesn't answer reads them (`_lib/search.ts`). Unset = none are built, never probed. */
  SEARCH_SIDECARS?: string
  /** `intervals`: the dates the interval store's generation holds read their path store from the change-interval
   *  store on `INDEX_R2` (`index.ts` `openInterval`, specs/interval-store.md); `opt-in`: only requests
   *  with `ps=iv` (`withPathStore`); unset = per-scan stores. */
  PATH_STORE?: string
  /** `1`: Δ-mode diffs (`dm=delta`, unscoped) by lazy descent over the interval store (`_lib/diffDescent.ts`; its
   *  planner hands busy, broad paths to the as-of walk). Needs no index beyond the store's. */
  DIFF_DESCENT?: string
  /** `1`: the fleet-root owner series reads one bounded interval-store version range (`readRootHistory`),
   *  the canonical reader as fallback. A prototype, off (specs/root-owner-history.md). */
  OWNER_HISTORY_BATCH?: string
  /** The interval store generation (`interval-store/<gen>/` on `INDEX_R2`), a pin; unset: the store's current one,
   *  `interval-store/CURRENT.json` (`index.ts` `ivGen`), which a compaction moves. */
  INTERVAL_STORE_GEN?: string
  /** Store-relative namespace; secondary and age stores use independent pointers and generations. */
  INTERVAL_STORE_PREFIX?: string
  /** Root holding the age histogram's interval store (without the trailing `interval-store`). */
  AGE_STORE_PREFIX?: string
  /** A revision of the generation's files (rewritten in place): keys every cache over them apart. */
  INTERVAL_STORE_REV?: string
  /** The static name index's generation in `INDEX_R2` (`static-names/<gen>/`): a pin; unset, the primary's is
   *  `static-names/CURRENT.json`'s (`index.ts` `withStaticGen`, resolved per request). Required (pinned or pointed) with
   *  `FILTER_STATIC` / `NAME_SUMMARY_STATIC`. */
  STATIC_GEN?: string
  /** Optional independent pointer for this static lineage; a STATIC_GEN pin still wins. */
  STATIC_CURRENT_KEY?: string
  STORE_ACCESS_KEY_ID?: string
  STORE_SECRET_ACCESS_KEY?: string
  /** Global second cache tier behind the colo cache (`_lib/edgeCache.ts`). */
  CACHE_KV?: KVNamespace
  /** The response cache's deployment environment (`_lib/edgeCache.ts` `cacheEnvTag`): set, it is the tag
   *  (`""` = prod's keys); unset, the request host, except `PROD_HOST` itself (prod's keys). */
  CACHE_NS?: string
  /** This deployment's production host (also build-time: `vite.config.ts` → `src/hosts.ts`). */
  PROD_HOST?: string
  /** The scope every viewer of this deployment needs (`gcs` | `cw`). */
  BASE_SCOPE?: string
  /** Public/no-gate deploys (r2.rbw.sh, per-project embeds): grant the base
   *  viewer scope anonymously so read endpoints serve without auth. Admin and
   *  any non-base scope still require a real identity, so mutations stay closed
   *  (specs/federated-scans.md). */
  PUBLIC_READ?: string
  /** The store root's crumb label (e.g. `my GCS`); unset = `all buckets`. */
  ROOT_LABEL?: string
  /** The root crumb's tooltip: what the store root covers (e.g. which buckets), in words. */
  ROOT_TITLE?: string
  /** Snapshot dir of this store inside the data bucket (`snapshots/<sub>/`); unset = the bare `snapshots/`. */
  SNAPSHOTS_SUBDIR?: string
  /** Dedicated SA key (Batch submit + actAs the job SA) for the sweep dispatch bridge. */
  GCP_SA_KEY?: string
  /** The service account dispatched Batch jobs run as (a `[vars]` entry). */
  JOB_SA?: string
  /** Secondary stores served beside the primary (`_lib/stores.ts`,
   *  specs/done/multi-store.md): `{<key>: {scope?, vars?, secrets?}}` as JSON. */
  STORES_JSON?: string
  /** Set only on a secondary store's env overlay (`withStore`): its key
   *  (`index_schema.store`, cache keys); unset = the primary. */
  STORE_KEY?: string
  /** Set only on a secondary store's overlay: a scope its viewers need on top
   *  of the deployment's viewer scope (`requireViewer`). */
  STORE_SCOPE?: string
  /** The footer LRU's budget (`_lib/footerCache.ts`, specs/done/footer-lru.md): the most
   *  `index_row_groups` rows its hot entries hold (default 1,000,000)… */
  FOOTER_CACHE_MAX_ROWS?: string
  /** …and, when set, the most bytes (their text columns + 64 per row; unset = no byte cap). */
  FOOTER_CACHE_MAX_BYTES?: string
  /** The Analytics Engine dataset the usage points go to (`_lib/usage.ts`, specs/usage-metrics.md); unset = none. */
  USAGE?: AnalyticsEngineDataset
}

export interface Ctx {
  request: Request
  env: Env
  /** Pages' per-request object, shared by the middleware and the route (each gets its own `Request` copy). */
  data?: Record<string, unknown>
}

export const GCS_SCOPE = 'gcs'
export const CW_SCOPE = 'cw'
export const ADMIN_SCOPE = 'admin'

/** The deployment's base scope: what `requireViewer` asks for. */
export const baseScope = (env: Env): string => env.BASE_SCOPE ?? GCS_SCOPE
/** The read-only viewer tier (`gcs:read` / `cw:read`): every read endpoint
 *  admits it, no write does. What a share link mints by default
 *  (specs/share-link-hardening.md). */
export const baseReadScope = (env: Env): string => `${baseScope(env)}:read`
/** Ownership writes (`gcs:assign` / `cw:assign`): assigning any prefix to anyone
 *  (or clearing it). Every signed-in full viewer gets it from the policy, and
 *  their personal agent token carries it; a share link doesn't, unless an admin
 *  mints one with it. */
export const baseAssignScope = (env: Env): string => `${baseScope(env)}:assign`

/** Staff by email domain (`STAFF_DOMAIN`). Unset or empty = nobody is staff by
 *  domain (an empty domain would otherwise match every address). */
const isStaff = (env: Env, email: string): boolean => {
  const d = env.STAFF_DOMAIN?.trim().toLowerCase()
  return !!d && email.toLowerCase().endsWith(`@${d}`)
}

const viewerDomains = (env: Env): string[] =>
  (env.VIEWER_DOMAINS ?? '').split(',').map(d => d.trim().toLowerCase()).filter(Boolean)

/** Whether admin is the explicit list alone (`ADMIN_LIST`). */
export const adminListOn = (env: Env): boolean => !!env.ADMIN_LIST

/** The break-glass admins (`ADMIN_BOOTSTRAP`), lower-cased; none unless the list is on. */
export const bootstrapAdmins = (env: Env): string[] =>
  adminListOn(env) ? (env.ADMIN_BOOTSTRAP ?? '').split(',').map(e => e.trim().toLowerCase()).filter(e => e.includes('@')) : []

/** Whether this deployment reads `admin_emails` at all: the list is on, or `ADMIN_EMAILS` says the table exists. */
const readsAdminTable = (env: Env): boolean => !!env.DB && (adminListOn(env) || !!env.ADMIN_EMAILS)

const noAdminTable = (e: unknown): boolean => /no such table: admin_emails/.test(String((e as Error)?.message ?? e))

/** Whether `email` (lower-cased) has an `admin_emails` row. With the list on, a database that hasn't the table yet
 *  (its recipe unapplied) has no rows: sign-in must not break on it, and nobody becomes an admin by it. */
async function adminRow(env: Env, email: string): Promise<boolean> {
  if (!readsAdminTable(env)) return false
  try {
    return !!(await env.DB!.prepare('SELECT email FROM admin_emails WHERE email = ?').bind(email).first())
  } catch (e) {
    if (!adminListOn(env) || !noAdminTable(e)) throw e
    console.warn('ADMIN_LIST is on and `admin_emails` does not exist: nobody is an admin by the list')
    return false
  }
}

/** One `admin_emails` row: who is listed, with who added them and when, and their GitHub handle where known (what
 *  `dt-cloud admins check` compares with a GitHub org's owners). */
export interface AdminRow { email: string; note: string | null; who: string; ts: number; github: string | null }

/** The deployment's email admins, as `/admin` shows them (`/api/admins`): the rule in force (`list`, else the staff
 *  domain), the table's state and rows, the break-glass addresses, and `none`: the list is on and names nobody, so
 *  no sign-in is an admin until `ADMIN_BOOTSTRAP` names one (or a row is written to the database). */
export interface AdminRoster {
  list: boolean
  staffDomain: string | null
  table: 'ok' | 'missing' | 'unused'
  rows: AdminRow[]
  /** Whether the table has its `github` column; without it (the `admin-list` recipe unapplied) rows read `github: null`
   *  and `/api/admins` adds, removes and edits notes, but stores no handle. */
  handles: boolean
  bootstrap: string[]
  none: boolean
}

/** How long a database seen without `admin_emails.github` is taken to still lack it, so the recipe's arrival is seen
 *  with no deploy. */
const NO_GITHUB_MS = 60_000
/** Per isolate and D1 binding: when the list's read last found no `github` column. No `PRAGMA`: the read a request
 *  makes anyway is the probe, so a database with the column pays nothing, and one without pays one failed statement a
 *  minute. (A binding object not reused across requests only loses the memo: that request's read fails once more.) */
const noGithubAt = new WeakMap<object, number>()

export async function adminRoster(env: Env): Promise<AdminRoster> {
  const list = adminListOn(env)
  const bootstrap = bootstrapAdmins(env)
  const staffDomain = list ? null : env.STAFF_DOMAIN?.trim().toLowerCase() || null
  let table: AdminRoster['table'] = 'unused'
  let rows: AdminRow[] = []
  let handles = false
  if (readsAdminTable(env)) {
    const db = env.DB!
    const read = (cols: string) => db.prepare(`SELECT ${cols} FROM admin_emails ORDER BY email`).all<AdminRow>()
    const readOld = async () => (await read('email, note, who, ts')).results.map(r => ({ ...r, github: null }))
    const missed = noGithubAt.get(db)
    try {
      if (missed !== undefined && Date.now() - missed < NO_GITHUB_MS) rows = await readOld()
      else {
        rows = (await read('email, note, who, ts, github')).results
        handles = true
      }
      table = 'ok'
    } catch (e) {
      // A table from before the `github` column (the `admin-list` recipe unapplied): the same admins, no handles.
      if (/no such column: github/.test(String((e as Error)?.message ?? e))) {
        noGithubAt.set(db, Date.now())
        rows = await readOld()
        table = 'ok'
      } else if (noAdminTable(e)) table = 'missing'
      else throw e
    }
  }
  return { list, staffDomain, table, rows, handles, bootstrap, none: list && !rows.length && !bootstrap.length }
}

/**
 * Email → scopes: the in-app policy that the Access policy used to be. Staff
 * get everything (`admin` only where `isAdmin` says so: always, unless
 * `ADMIN_LIST`); a viewer domain (`VIEWER_DOMAINS`) or a D1 `allowed_emails`
 * row (the app-owned allowlist — see /admin/db) gets the base scope + assign
 * (a `read_only` row: the read-only tier alone), plus
 * `admin` for an admin (`isAdmin`); with `ADMIN_LIST`, a listed address is
 * admitted by the list itself. Email sessions re-derive scopes here on
 * every request, so removing a row de-authorizes existing sessions on their
 * next request. If the DB isn't bound (local dev), non-staff fall back to
 * allowed — local dev has no gate to enforce.
 */
/** `email`'s `allowed_emails` row as `{ read_only }`, or null. A D1 that
 * hasn't applied (or dropped) the `read_only` migration still admits its
 * rows, as full viewers — sign-in must not break on a schema a branch
 * chose not to carry. */
export async function allowedRow(db: D1Database, email: string): Promise<{ read_only: number } | null> {
  try {
    return await db.prepare('SELECT read_only FROM allowed_emails WHERE email = ?').bind(email).first<{ read_only: number }>()
  } catch (e) {
    if (!/no such column: read_only/.test(String((e as Error).message))) throw e
    const row = await db.prepare('SELECT email FROM allowed_emails WHERE email = ?').bind(email).first()
    return row ? { read_only: 0 } : null
  }
}

export const scopesFor = (env: Env) => async (raw: string): Promise<string[] | null> => {
  const email = raw.toLowerCase()
  const viewer = [baseScope(env), baseAssignScope(env)]
  if (isStaff(env, email)) return (await isAdmin(env, email)) ? allScopes(env) : allScopes(env).filter(s => s !== ADMIN_SCOPE)
  if (!env.DB) return adminListOn(env) && (await isAdmin(env, email)) ? [...viewer, ADMIN_SCOPE] : viewer
  const byDomain = viewerDomains(env).some(d => email.endsWith(`@${d}`))
  const row = byDomain ? null : await allowedRow(env.DB, email)
  // The list admits whom it names: an admin who couldn't sign in would be no admin.
  if (!byDomain && !row) return adminListOn(env) && (await isAdmin(env, email)) ? [...viewer, ADMIN_SCOPE] : null
  if (await isAdmin(env, email)) return [...viewer, ADMIN_SCOPE]
  // A `read_only` row is the read-only viewer tier (what a read-only share link carries).
  return row?.read_only ? [baseReadScope(env)] : viewer
}

export const ADMIN_GRANT_REFUSED = 'with `ADMIN_LIST` on, a grant can\'t carry `admin` or `*`: add the person to the admin list instead'
export const ADMIN_ROTATE_REFUSED = 'with `ADMIN_LIST` on, an admin grant can\'t be re-keyed: revoke it, and add the person to the admin list instead'
export const ADMIN_WIDEN_REFUSED = 'with `ADMIN_LIST` on, an admin grant can only lose access: it can\'t be re-enabled, or given a later expiry, a higher redemption cap or sessions that outlive its expiry. Add the person to the admin list instead'

export const GRANT_TERMS_REFUSED = 'a grant\'s `expiresAt`, `maxRedeems` and `sessionTtlS` are numbers or null'

/** A write `grantStore` refused; its message is the answer (`/api/auth/*`: a 403). */
export class AdminGrantRefused extends Error {}

/** A grant's terms `grantStore` refused as malformed; its message is the answer (`/api/auth/*`: a 400). */
export class BadGrantTerms extends Error {}

/** Refuse a term that isn't a number or null. The package's routes store what they're given: a `PATCH`'s `expiresAt` as
 *  is, a mint's `expiresInS` added to the clock (`'x'` concatenates). A stored string compares as never reached, so that
 *  link would outlive any expiry it was given (and an expired one would revive). */
function checkTerms(terms: { expiresAt?: unknown; maxRedeems?: unknown; sessionTtlS?: unknown }): void {
  const ok = (v: unknown) => v === null || v === undefined || (typeof v === 'number' && Number.isFinite(v))
  if (![terms.expiresAt, terms.maxRedeems, terms.sessionTtlS].every(ok)) throw new BadGrantTerms(GRANT_TERMS_REFUSED)
}

/** The scopes as the `grants` column will read back: it splits on spaces and commas, so `gcs admin` is two. */
const carriesAdmin = (scopes: unknown): boolean => parseScopes(String(scopes)).some(s => s === ADMIN_SCOPE || s === '*')

/** Whether `next` loosens a limit that `null` lifts (an expiry, a redemption cap). What isn't a number loosens: a stored
 *  string compares as never reached. */
const loosens = (cur: number | null, next: unknown): boolean => {
  if (next === null || next === undefined) return cur !== null
  return typeof next !== 'number' || !Number.isFinite(next) || (cur !== null && next > cur)
}

/** Whether `patch` gives `grant` anything back: what ended it (an expiry, an exhausted cap) reopens. `sessionTtlS` isn't
 *  here: it reopens nothing, and applies only to later redemptions. */
const widens = (grant: Grant, patch: GrantPatch): boolean =>
  ('expiresAt' in patch && loosens(grant.expiresAt, patch.expiresAt))
  || ('maxRedeems' in patch && loosens(grant.maxRedeems, patch.maxRedeems))
  // Sessions that outlive the expiry: an expired grant's come back (`sessionValid`).
  || ('expiryEndsSessions' in patch && grant.expiryEndsSessions && !patch.expiryEndsSessions)

/**
 * The gate's grant store. Every grant's terms are numbers or null (`checkTerms`). With `ADMIN_LIST` no new credential carries `admin` or `*`: a grant minted with one would be an
 * admin whatever the list says, and could edit it. Here rather than in the routes because every path to a grant ends
 * here, the package's included (`POST /api/auth/grants`, a request's approval, `/api/token`). Reads are untouched, so a
 * grant already holding the scope still authenticates, stays on `/admin`, and can be revoked.
 *
 * - `insert`: refused, except the grant the package mints by itself for an address the policy admits
 *   (`POST /api/auth/request`, `createdBy: 'policy'`), whose asker can't be told no: it goes in without the scope.
 * - `rotate`: refused for an admin grant: re-keying hands out a new secret carrying the scope.
 * - `update`, `setDisabled`: an admin grant can lose access but not regain it (`widens`; re-enabling): an expired or
 *   disabled one is an admin again otherwise. Shortening an expiry and disabling pass. Neither touches `revoked_at`, and
 *   the store's `setDisabled` refuses a revoked grant, so a revoke stays revoked.
 */
function grantStore(env: Env): GrantStore {
  const d1 = d1GrantStore(env.DB!)
  const store: GrantStore = {
    ...d1,
    async insert(grant, tokenHash) {
      checkTerms(grant)
      return d1.insert(grant, tokenHash)
    },
    async update(id, patch) {
      checkTerms(patch)
      return d1.update(id, patch)
    },
  }
  if (!adminListOn(env)) return store
  return {
    ...store,
    async insert(grant, tokenHash) {
      if (!carriesAdmin(grant.scopes)) return store.insert(grant, tokenHash)
      if (grant.createdBy !== 'policy') throw new AdminGrantRefused(ADMIN_GRANT_REFUSED)
      return store.insert({ ...grant, scopes: parseScopes(String(grant.scopes)).filter(s => !carriesAdmin(s)) }, tokenHash)
    },
    async rotate(id, newTokenHash, sessionsInvalidBefore) {
      const grant = await store.byId(id)
      if (grant && carriesAdmin(grant.scopes)) throw new AdminGrantRefused(ADMIN_ROTATE_REFUSED)
      return store.rotate(id, newTokenHash, sessionsInvalidBefore)
    },
    async update(id, patch) {
      const grant = await store.byId(id)
      if (grant && carriesAdmin(grant.scopes) && widens(grant, patch)) throw new AdminGrantRefused(ADMIN_WIDEN_REFUSED)
      return store.update(id, patch)
    },
    async setDisabled(id, nowS) {
      if (nowS === null) {
        const grant = await store.byId(id)
        if (grant && grant.disabledAt !== null && carriesAdmin(grant.scopes)) throw new AdminGrantRefused(ADMIN_WIDEN_REFUSED)
      }
      return store.setDisabled(id, nowS)
    },
  }
}

export function gateFor(env: Env): Gate | null {
  if (!env.DB || !env.SESSION_SECRET) return null
  return createGate({
    store: grantStore(env),
    requests: d1RequestStore(env.DB),
    audit: d1AuditSink(env.DB),
    // The name + face Google verified at sign-in (seeded by default),
    // or a self-set one: what `whoami.subject` carries to the header chip.
    profiles: d1ProfileStore(env.DB),
    secret: env.SESSION_SECRET,
    policy: scopesFor(env),
    cookieName: SESSION_COOKIE,
  })
}

/** The session cookie the gate reads (`presentsCredentials` verifies it). */
export const SESSION_COOKIE = DEFAULT_COOKIE_NAME

/** Identity + scopes however the request proved them; null if it didn't. */
export interface Identity {
  email: string | null
  /** Grant display name, for grant sessions with no email. */
  name: string | null
  scopes: string[]
  /** `admin` scope, as a flag — what the plan-first sweep console keys on. */
  admin: boolean
  via: 'session' | 'grant' | 'public'
  /** The gate's subject (profile name + inlined avatar), for the header chip. */
  subject: Subject | null
  /** The grant's id, for grant sessions (an activity actor names an unnamed grant by it). */
  grantId?: string
}
const withAdmin = (id: Omit<Identity, 'admin'>): Identity => ({ ...id, admin: id.scopes.includes(ADMIN_SCOPE) || id.scopes.includes('*') })

/**
 * Whether `email` is an admin of this deployment: the one answer every email identity's admin comes from (`scopesFor`
 * puts the `admin` scope on a session by it, so `requireAdmin`, `Identity.admin`, whoami and the client follow; a
 * Slack click asks it directly, `slack/actions.ts`). Where admins come from is this function's alone to change.
 *
 * - `ADMIN_LIST`: the explicit list (an `admin_emails` row) or the break-glass `ADMIN_BOOTSTRAP`. Never the staff
 *   domain, and never a fallback to it: an empty or missing list means no admins (`adminRoster().none`).
 * - otherwise: the staff domain, or an `admin_emails` row where `ADMIN_EMAILS` says the table exists.
 */
export async function isAdmin(env: Env, raw: string): Promise<boolean> {
  const email = raw.toLowerCase()
  if (adminListOn(env)) return bootstrapAdmins(env).includes(email) || adminRow(env, email)
  return isStaff(env, email) || adminRow(env, email)
}

function authIdentity(auth: Auth): Identity {
  if (auth.kind === 'sso') return withAdmin({ email: auth.email, name: null, scopes: auth.scopes, via: 'session', subject: auth.subject })
  return withAdmin({ email: auth.grant.email ?? null, name: auth.grant.name ?? null, scopes: auth.scopes, via: 'grant', subject: auth.grant.subject, grantId: auth.grant.id })
}

/** All app scopes (staff, and the local-dev identity so `/data` etc. work
 *  without a minted session): the deployment's base scope — which a store
 *  other than gcs/cw (e.g. `laptop`) names itself — plus the fixed ones. */
const allScopes = (env: Env): string[] =>
  [...new Set([GCS_SCOPE, CW_SCOPE, ADMIN_SCOPE, baseScope(env), baseAssignScope(env)])]

/** Per request and env: its identity lookup (a D1 read for a session or a grant), so the middleware's gate
 *  (`restrictGate`) and the route, handed the same Request and env, share one. The env is part of the key: the identity
 *  depends on its database, secret and scope policy, and a union member's env (`withStore`) is its own. */
const identified = new WeakMap<Request, WeakMap<object, Promise<Identity | null>>>()

export function identify(ctx: Ctx): Promise<Identity | null> {
  let byEnv = identified.get(ctx.request)
  if (!byEnv) identified.set(ctx.request, byEnv = new WeakMap())
  const env = ctx.env as unknown as object
  const hit = byEnv.get(env)
  if (hit) return hit
  const id = identifyOnce(ctx)
  byEnv.set(env, id)
  return id
}

async function identifyOnce(ctx: Ctx): Promise<Identity | null> {
  // Local dev has no minted session, so `wrangler pages
  // dev` requests would 401 — leaving the client `DEV_IDENTITY` stub (which
  // only fakes the *client's* whoami) disagreeing with the server. Treat any
  // request whose host is localhost as a full-scope dev user so the two agree.
  // This can't reach prod: Cloudflare routes by the real hostname, so a
  // gcs.oa.dev request's URL host is never `localhost`/`127.0.0.1`.
  const host = new URL(ctx.request.url).hostname
  if (host === 'localhost' || host === '127.0.0.1') {
    meterOf(ctx)?.identified('session', 'session')
    return withAdmin({ email: ctx.env.DEV_EMAIL ?? 'dev@example.test', name: null, scopes: allScopes(ctx.env), via: 'session', subject: null })
  }
  const gate = gateFor(ctx.env)
  if (!gate) return null
  const auth = await gate.authenticate(ctx.request)
  if (!auth) return null
  // The usage point's caller (`_lib/usage.ts`): what this lookup just proved, so the middleware needs none of its own.
  const m = meterOf(ctx)
  if (m) {
    if (auth.kind === 'sso') m.identified('session', 'session')
    else { const c = grantCaller(ctx.request, auth.grant); m.identified(c.via, c.caller) }
  }
  return authIdentity(auth)
}

/** Whether a request may pass the gate before it is asked: a public deployment, local dev, or a session cookie
 *  (`SESSION_COOKIE`) that verifies — the package's `verifySessionClaims` (its HMAC signature under the gate's
 *  `SESSION_SECRET`, `crypto.subtle`, no I/O) with its expiry not past. Nothing else starts what a route starts beside
 *  its gate (`shared.ahead`), which then waits for it: no credential, other cookies, a forged, malformed or expired
 *  session, a `Basic` header — and a `Bearer` token or `?key=` link, which only the gate's D1 lookup can check (agent and
 *  API calls, not the browser's latency-sensitive path; the gate reads a presented token first, so a cookie beside one
 *  isn't the credential). Only a session minted under the secret can make a refused request cost more than the gate's
 *  own reads (`AHEAD_COST`, `GATE_COST`). */
export async function presentsCredentials(ctx: Ctx): Promise<boolean> {
  const url = new URL(ctx.request.url)
  if (ctx.env.PUBLIC_READ || url.hostname === 'localhost' || url.hostname === '127.0.0.1') return true
  if (/^Bearer (.+)$/.test(ctx.request.headers.get('authorization') ?? '') || url.searchParams.get('key')) return false
  const cookie = readCookie(ctx.request, SESSION_COOKIE)
  // No gate (`gateFor`), no pass.
  if (!cookie || !ctx.env.DB || !ctx.env.SESSION_SECRET) return false
  return (await verifySessionClaims(cookie, ctx.env.SESSION_SECRET, Date.now())) !== null
}

/** Gate a handler on a scope; 401/403 as JSON. */
export async function requireScope(ctx: Ctx, scope: string): Promise<Identity | Response> {
  // Public deploy: the base viewer scope is granted anonymously so read
  // endpoints serve without auth. A real identity (if the deploy also has a
  // gate) still wins; any non-base scope (admin, other stores) falls through
  // to the normal gate, so mutations stay closed (specs/federated-scans.md).
  if (ctx.env.PUBLIC_READ && scope === baseScope(ctx.env)) {
    const id = await identify(ctx)
    return id ?? { email: null, name: null, scopes: [baseScope(ctx.env)], admin: false, via: 'public', subject: null }
  }
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  if (!id.scopes.includes(scope) && !id.scopes.includes('*')) return json({ error: 'forbidden' }, 403)
  return id
}

export { hasScope }

export async function requireAnyScope(ctx: Ctx, scopes: string[]): Promise<Identity | Response> {
  if (ctx.env.PUBLIC_READ && scopes.includes(baseScope(ctx.env))) return requireScope(ctx, baseScope(ctx.env))
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  if (!scopes.some(sc => id.scopes.includes(sc)) && !id.scopes.includes('*')) return json({ error: 'forbidden' }, 403)
  return id
}
/** Any authenticated viewer of this deployment (reads): the full viewer scope
 *  or the read-only tier. */
export async function requireViewer(ctx: Ctx): Promise<Identity | Response> {
  const id = await requireAnyScope(ctx, [baseScope(ctx.env), baseReadScope(ctx.env)])
  // A secondary store may demand more (its `STORES_JSON` `scope`, e.g.
  // staff-only `admin`); the identity is the deployment's either way.
  const extra = ctx.env.STORE_SCOPE
  if (id instanceof Response || !extra || id.scopes.includes(extra) || id.scopes.includes('*')) return id
  return id.via === 'public' ? json({ error: 'unauthenticated' }, 401) : json({ error: 'forbidden' }, 403)
}
/** Staging (the opt-in trash proposal) and other non-admin writes: the full
 *  base scope — a read-only guest link can't. */
export const requireStager = (ctx: Ctx): Promise<Identity | Response> => requireScope(ctx, baseScope(ctx.env))
/** Ownership writes (`POST /api/actions`): the assign scope, or an admin. */
export const requireAssigner = (ctx: Ctx): Promise<Identity | Response> =>
  requireAnyScope(ctx, [baseAssignScope(ctx.env), ADMIN_SCOPE])
/** An admin (plan writes + sweep dispatch). */
export const requireAdmin = (ctx: Ctx): Promise<Identity | Response> => requireScope(ctx, ADMIN_SCOPE)

export const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data) + '\n', {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', ...headers },
  })
