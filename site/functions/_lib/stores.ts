/**
 * Multi-store deploys (specs/multi-store.md): one deployment serves its
 * **primary** store plus any secondary stores named in `STORES_JSON`.
 *
 * Every data Function resolves its store from an explicit `store=<key>` query
 * param. No param (or `store=primary`) returns the request's context
 * untouched, so an existing single-store deploy behaves exactly as before:
 * today's single-store vars (`SNAPSHOTS_SUBDIR`, `STORE_SCHEME`,
 * `STORE_BUCKETS`, `STORE_PREFIXES`, `STORE_BUCKET`/`STORE_ENDPOINT`/
 * `STORE_REGION` + creds, `ROOT_LABEL`) remain the primary's.
 *
 * A secondary store gets an `Env` overlay: every per-store var is cleared
 * (nothing leaks from the primary; the GCS HMAC fallback creds included),
 * then set from its config, so the rest of the Functions read `env.STORE_*`
 * as they always have. `STORE_KEY` names the store for the D1 queries
 * (`index_schema.store`) and the cache keys; `STORE_SCOPE` is an extra scope
 * `requireViewer` demands on top of the deployment's viewer scope.
 *
 *   STORES_JSON = {"meta": {
 *     "scope": "admin",
 *     "vars": {"ROOT_LABEL": "our storage", "SNAPSHOTS_SUBDIR": "meta", "STORE_PREFIXES": "meta-l2/,snapshots/meta/"},
 *     "secrets": {"STORE_ACCESS_KEY_ID": "STORE_META_ACCESS_KEY_ID", "STORE_SECRET_ACCESS_KEY": "STORE_META_SECRET_ACCESS_KEY"}
 *   }}
 *
 * `secrets` maps a per-store var to the NAME of a Pages secret holding its
 * value, so no secret sits in the (public) `wrangler.toml`.
 */
import type { Env } from './auth.js'

/** `index_schema.store` of the primary store's rows (a fixed sentinel: one
 * migration lineage serves several deploys, each with its own `STORE`). */
export const PRIMARY_STORE = 'primary'

/** The vars a store config may set; all are cleared for a secondary store. */
export const STORE_VARS = [
  'ROOT_LABEL',
  'SNAPSHOTS_SUBDIR',
  'STORE_SCHEME',
  'STORE_BUCKETS',
  'STORE_PREFIXES',
  'STORE_BUCKET',
  'STORE_ENDPOINT',
  'STORE_REGION',
  'STORE_ACCESS_KEY_ID',
  'STORE_SECRET_ACCESS_KEY',
] as const
export type StoreVar = typeof STORE_VARS[number]
/** Cleared too (never set): the primary's GCS creds fallback (`storeCreds`). */
const CLEARED = [...STORE_VARS, 'GCS_HMAC_KEY_ID', 'GCS_HMAC_SECRET'] as const

export interface StoreConfig {
  /** A scope viewers need on top of the deployment's viewer scope. */
  scope?: string
  vars?: Partial<Record<StoreVar, string>>
  /** Per-store var → name of the secret (env binding) holding its value. */
  secrets?: Partial<Record<StoreVar, string>>
}

const KEY_RE = /^[a-z0-9][a-z0-9-]*$/

export class StoreConfigError extends Error {}

let parsed: { raw: string; stores: Record<string, StoreConfig> } | null = null

/** The secondary stores (`STORES_JSON`), validated; `{}` when unset. */
export function secondaryStores(env: Env): Record<string, StoreConfig> {
  const raw = env.STORES_JSON ?? ''
  if (!raw) return {}
  if (parsed?.raw === raw) return parsed.stores
  let obj: unknown
  try {
    obj = JSON.parse(raw)
  } catch (e) {
    throw new StoreConfigError(`STORES_JSON: ${(e as Error).message}`)
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new StoreConfigError('STORES_JSON: want an object of store configs')
  const stores: Record<string, StoreConfig> = {}
  for (const [key, cfg] of Object.entries(obj as Record<string, unknown>)) {
    if (!KEY_RE.test(key) || key === PRIMARY_STORE) throw new StoreConfigError(`STORES_JSON: bad store key '${key}'`)
    if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) throw new StoreConfigError(`STORES_JSON.${key}: want an object`)
    const { scope, vars, secrets, ...rest } = cfg as Record<string, unknown>
    if (Object.keys(rest).length) throw new StoreConfigError(`STORES_JSON.${key}: unknown field(s) ${Object.keys(rest).join(', ')}`)
    if (scope !== undefined && typeof scope !== 'string') throw new StoreConfigError(`STORES_JSON.${key}.scope: want a string`)
    for (const [field, m] of [['vars', vars], ['secrets', secrets]] as const) {
      if (m === undefined) continue
      if (!m || typeof m !== 'object' || Array.isArray(m)) throw new StoreConfigError(`STORES_JSON.${key}.${field}: want an object`)
      for (const [k, v] of Object.entries(m)) {
        if (!(STORE_VARS as readonly string[]).includes(k)) throw new StoreConfigError(`STORES_JSON.${key}.${field}: unknown var '${k}'`)
        if (typeof v !== 'string') throw new StoreConfigError(`STORES_JSON.${key}.${field}.${k}: want a string`)
      }
    }
    stores[key] = cfg as StoreConfig
  }
  parsed = { raw, stores }
  return stores
}

/** The store a (possibly overlaid) env serves: `PRIMARY_STORE` or a key. */
export const storeKey = (env: Env): string => env.STORE_KEY ?? PRIMARY_STORE
export const isPrimary = (env: Env): boolean => storeKey(env) === PRIMARY_STORE

/** An index variant as D1 records it (`index_schema` / `index_row_groups`):
 * the primary's as-is, a secondary store's as `<store>:<variant>`
 * (`dt_cloud.index_footer.d1_variant`). The primary's queries are exactly
 * what they were before stores existed (no `store` predicate, so they run on
 * an un-migrated D1), and always name a bare variant, so they never see a
 * secondary store's rows; a secondary store's queries also say `store = ?`,
 * which fails loudly until the store migration is applied. */
export const d1Variant = (env: Env, variant: string): string => (isPrimary(env) ? variant : `${storeKey(env)}:${variant}`)

/** `env` as secondary store `key` sees it (see the module comment). */
export function storeEnv(env: Env, key: string, cfg: StoreConfig): Env {
  const out: Record<string, unknown> = { ...env }
  for (const k of CLEARED) delete out[k]
  Object.assign(out, cfg.vars ?? {})
  for (const [k, name] of Object.entries(cfg.secrets ?? {})) {
    const v = (env as unknown as Record<string, unknown>)[name!]
    if (typeof v === 'string') out[k] = v
  }
  out.STORE_KEY = key
  if (cfg.scope) out.STORE_SCOPE = cfg.scope
  return out as Env
}

/** The 400 body for a `lens=user:` on a secondary store: the user lens folds
 * the ownership ledger, which is the primary store's. */
export const LENS_PRIMARY_ONLY = 'lens is the primary store’s (ownership ledger)'

const jsonErr =(error: string, status: number): Response =>
  new Response(JSON.stringify({ error }) + '\n', { status, headers: { 'content-type': 'application/json; charset=utf-8' } })

/** The `store=` param as given: null = the primary. */
export function requestedStore(request: Request): string | null {
  const raw = new URL(request.url).searchParams.get('store')
  return raw === null || raw === '' || raw === PRIMARY_STORE ? null : raw
}

/**
 * Resolve the request's store: the context itself for the primary (no
 * `store=`), else a copy whose `env` is that store's overlay; a 400/404/500
 * `Response` for a malformed, unknown or misconfigured store.
 */
export function withStore<C extends { request: Request; env: Env }>(ctx: C): C | Response {
  const key = requestedStore(ctx.request)
  if (key === null) return ctx
  if (!KEY_RE.test(key)) return jsonErr('bad store', 400)
  let stores: Record<string, StoreConfig>
  try {
    stores = secondaryStores(ctx.env)
  } catch (e) {
    return jsonErr((e as Error).message, 500)
  }
  const cfg = stores[key]
  if (!cfg) return jsonErr(`unknown store '${key}'`, 404)
  // `waitUntil` (Pages' EventContext) stays bound to the real context.
  const w = (ctx as { waitUntil?: unknown }).waitUntil
  return { ...ctx, env: storeEnv(ctx.env, key, cfg), ...(typeof w === 'function' ? { waitUntil: w.bind(ctx) } : {}) }
}

/** For the primary-only surfaces (the ownership ledger: owners, estate,
 * assignments, claims, actions): a 404 for any other store, null otherwise. */
export function primaryOnly(ctx: { request: Request }): Response | null {
  const key = requestedStore(ctx.request)
  return key === null ? null : jsonErr(`store '${key}' has no ownership ledger (primary store only)`, 404)
}
