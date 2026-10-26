/**
 * Multi-store deploys (specs/done/multi-store.md): one deployment serves its
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
 *
 * `path` is the store's page path: the URL prefix its pages live under, the frontend's `Store.path` for the same key
 * (mounted by `STORES_EXTRA`). The link unfurls (`og/serve.ts`) read it: a page below it is that store's (its
 * `ROOT_LABEL`, `STORE_SCHEME`, `STORE_ROOT` mount and data, the card's view carrying `store=<key>`). No `path`: the
 * store's pages get no card of their own. A store's mount is its own `STORE_ROOT` (`/`-separated; unset = `/`):
 *
 *   STORES_JSON = {"nas": {"path": "/nas", "vars": {"ROOT_LABEL": "nas", "STORE_ROOT": "mnt/user", …}}}
 *
 * The static name index is per store too: a store with its own sets
 * `STATIC_GEN` (its generation in the deployment's `INDEX_R2`) and, as its
 * generation was built, `FILTER_STATIC_HEAVY` / `FILTER_STATIC_ANCHORS`. One
 * without is `scan-not-indexed` under the deployment's static filter (the
 * inherited `FILTER_STATIC`), never answered from the primary's generation:
 *
 *   STORES_JSON = {"meta": {"vars": {…, "STATIC_GEN": "2026-10-14-meta"}}}
 *
 * An entry with `union` is a **union view** (specs/federated-scans.md, "First
 * live cases" 1), not a store: a read-time union of member stores' roots,
 * each member read at its own scan for the request's cutoff
 * (`_lib/union.ts`). `main: true` makes it what a request with no `store=`
 * reads (the main map); `store=primary` then still names the primary alone,
 * and `store=<view key>` names the view either way:
 *
 *   STORES_JSON = {"meta": {…}, "all": {"union": ["primary", "meta"], "main": true}}
 *
 * A view has no page path of its own: a main view's pages are the primary's (`/`), and a view's `store=` is reached
 * from those only.
 *
 * A view may say how the scan picker and the map mark each member (`sources`, keyed by member): a `label`
 * (default: the member's `ROOT_LABEL`, else its key) and an `icon` — an absolute `http(s)://` URL or a path the
 * site serves (`/…`). Each deployment sets its own; none is assumed. A site path must be a static asset the
 * Functions never see: under `/icons/` (excluded in `public/_routes.json`; anywhere else the catch-all answers it
 * with the SPA's `index.html` and the mark silently falls back to its label — `/api/union-scans` warns of one):
 *
 *   "all": {"union": ["primary", "meta"], "main": true,
 *           "sources": {"primary": {"label": "Buckets", "icon": "/icons/buckets.svg"}, "meta": {"icon": "https://example.com/meta.png"}}}
 */
import type { Env } from './auth.js'
import { withReq } from './shared.js'
import { meterOf } from './usage.js'

/** `index_schema.store` of the primary store's rows (a fixed sentinel: one
 * migration lineage serves several deploys, each with its own `STORE`). */
export const PRIMARY_STORE = 'primary'

/** The deployment's policy a secondary store inherits unless its config sets its own: whether the map's filter is the
 * static name index's (`FILTER_STATIC`), whether it is indexed-only (`FILTER_INDEXED_ONLY`), whether it answers path
 * terms (`SEARCH_PATH_TERMS`), the scan a view with no `?d=` opens on (`DEFAULT_SCAN`), and its link cards' anonymous tier (`OG_ANONYMOUS`: the store's pages unfurl as the
 * deployment's do, its own `scope` gating the pages, not the previews; a store sets `generic` to keep its own). */
export const INHERITED_VARS = ['FILTER_STATIC', 'FILTER_INDEXED_ONLY', 'SEARCH_PATH_TERMS', 'DEFAULT_SCAN', 'OG_ANONYMOUS'] as const

/** The vars a store config may set; all but `INHERITED_VARS` are cleared for a secondary store. The static name
 * index's are per store (`STATIC_GEN`, `FILTER_STATIC_HEAVY`, `FILTER_STATIC_ANCHORS`): a store reads a static
 * index only when its own config names a generation (in the deployment's `INDEX_R2`, `static-names/<gen>/`); with
 * the filter static and none named, it holds no scan (`staticFilter.ts` `UNINDEXED`: `scan-not-indexed`), never
 * the primary's generation. */
export const STORE_VARS = [
  'ROOT_LABEL',
  'ROOT_TITLE',
  'STORE_ROOT',
  'OG_DEFAULT_COLOR',
  'SNAPSHOTS_SUBDIR',
  'STORE_SCHEME',
  'STORE_BUCKETS',
  'STORE_PREFIXES',
  'STORE_BUCKET',
  'STORE_ENDPOINT',
  'STORE_REGION',
  'STORE_ACCESS_KEY_ID',
  'STORE_SECRET_ACCESS_KEY',
  'OBJECT_BUCKETS',
  'SERVED_R2',
  'STATIC_GEN',
  'STATIC_CURRENT_KEY',
  'PATH_STORE',
  'INTERVAL_STORE_GEN',
  'INTERVAL_STORE_PREFIX',
  'INTERVAL_STORE_REV',
  'AGE_STORE_PREFIX',
  'FILTER_STATIC_HEAVY',
  'FILTER_STATIC_ANCHORS',
  ...INHERITED_VARS,
] as const
export type StoreVar = typeof STORE_VARS[number]
/** Cleared (the inherited policy kept), plus, never set: the primary's GCS creds fallback (`storeCreds`) and its
 * name summaries' index (`/api/name-summary` serves the primary only). */
const CLEARED = [
  ...STORE_VARS.filter(k => !(INHERITED_VARS as readonly string[]).includes(k)),
  'GCS_HMAC_KEY_ID', 'GCS_HMAC_SECRET', 'NAME_SUMMARY_STATIC', 'STATIC_MAX_ROWS',
] as const

export interface StoreConfig {
  /** A scope viewers need on top of the deployment's viewer scope. */
  scope?: string
  vars?: Partial<Record<StoreVar, string>>
  /** Per-store var → name of the secret (env binding) holding its value. */
  secrets?: Partial<Record<StoreVar, string>>
  /** The store's page path (`/meta`): the URL prefix its pages live under (normalized: no trailing `/`). */
  path?: string
}

const KEY_RE = /^[a-z0-9][a-z0-9-]*$/

export class StoreConfigError extends Error {}

/** A union view (`STORES_JSON` entry with `union`): its member stores
 * (`PRIMARY_STORE` or secondary store keys, in config order), and whether it
 * is the main (no-`store=`) view. */
export interface UnionView {
  key: string
  members: string[]
  main: boolean
  /** How each member is marked (`sources` in its config): only the members it names. */
  sources: Record<string, SourceDisplay>
}

/** A union member's mark in the picker and on the map: its `label` and an optional `icon` (a URL or a site path). */
export interface SourceDisplay { label?: string; icon?: string }

/** A page path: `/`-led segments, no empty segment (a trailing `/` is dropped first). */
const PATH_RE = /^(\/[A-Za-z0-9._~-]+)+$/

const ICON_RE = /^(https?:\/\/[^\s]+|\/[^\s/][^\s]*)$/

let parsed: { raw: string; stores: Record<string, StoreConfig>; views: Record<string, UnionView> } | null = null

/** The secondary stores (`STORES_JSON`), validated; `{}` when unset. */
export function secondaryStores(env: Env): Record<string, StoreConfig> {
  return parseStores(env).stores
}

/** The union views (`STORES_JSON` entries with `union`), validated; `{}` when unset. */
export function unionViews(env: Env): Record<string, UnionView> {
  return parseStores(env).views
}

function parseStores(env: Env): { stores: Record<string, StoreConfig>; views: Record<string, UnionView> } {
  const raw = env.STORES_JSON ?? ''
  if (!raw) return { stores: {}, views: {} }
  if (parsed?.raw === raw) return parsed
  let obj: unknown
  try {
    obj = JSON.parse(raw)
  } catch (e) {
    throw new StoreConfigError(`STORES_JSON: ${(e as Error).message}`)
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new StoreConfigError('STORES_JSON: want an object of store configs')
  const stores: Record<string, StoreConfig> = {}
  const unions: Record<string, Record<string, unknown>> = {}
  for (const [key, cfg] of Object.entries(obj as Record<string, unknown>)) {
    if (!KEY_RE.test(key) || key === PRIMARY_STORE) throw new StoreConfigError(`STORES_JSON: bad store key '${key}'`)
    if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) throw new StoreConfigError(`STORES_JSON.${key}: want an object`)
    if ('union' in cfg) {
      unions[key] = cfg as Record<string, unknown>
      continue
    }
    const { scope, vars, secrets, path, ...rest } = cfg as Record<string, unknown>
    if (Object.keys(rest).length) throw new StoreConfigError(`STORES_JSON.${key}: unknown field(s) ${Object.keys(rest).join(', ')}`)
    if (scope !== undefined && typeof scope !== 'string') throw new StoreConfigError(`STORES_JSON.${key}.scope: want a string`)
    const page = typeof path === 'string' ? path.replace(/(.)\/$/, '$1') : path
    if (page !== undefined && (typeof page !== 'string' || !PATH_RE.test(page) || page.split('/').some(s => s === '.' || s === '..'))) throw new StoreConfigError(`STORES_JSON.${key}.path: want a page path like '/${key}' (not '/')`)
    const taken = Object.entries(stores).find(([, c]) => c.path === page)
    if (page !== undefined && taken) throw new StoreConfigError(`STORES_JSON.${key}.path: '${page}' is also '${taken[0]}'s`)
    for (const [field, m] of [['vars', vars], ['secrets', secrets]] as const) {
      if (m === undefined) continue
      if (!m || typeof m !== 'object' || Array.isArray(m)) throw new StoreConfigError(`STORES_JSON.${key}.${field}: want an object`)
      for (const [k, v] of Object.entries(m)) {
        if (!(STORE_VARS as readonly string[]).includes(k)) throw new StoreConfigError(`STORES_JSON.${key}.${field}: unknown var '${k}'`)
        if (typeof v !== 'string') throw new StoreConfigError(`STORES_JSON.${key}.${field}.${k}: want a string`)
      }
    }
    stores[key] = page === undefined ? cfg as StoreConfig : { ...(cfg as StoreConfig), path: page as string }
  }
  const views: Record<string, UnionView> = {}
  for (const [key, cfg] of Object.entries(unions)) {
    const { union, main, sources, ...rest } = cfg
    if (Object.keys(rest).length) throw new StoreConfigError(`STORES_JSON.${key}: unknown field(s) ${Object.keys(rest).join(', ')} (a union view takes union, main, sources)`)
    if (!Array.isArray(union) || union.length < 2 || union.some(m => typeof m !== 'string')) throw new StoreConfigError(`STORES_JSON.${key}.union: want ≥ 2 store keys`)
    const members = union as string[]
    if (new Set(members).size !== members.length) throw new StoreConfigError(`STORES_JSON.${key}.union: a store is named twice`)
    for (const m of members) if (m !== PRIMARY_STORE && !stores[m]) throw new StoreConfigError(`STORES_JSON.${key}.union: '${m}' is not a store (want '${PRIMARY_STORE}' or a store key)`)
    if (main !== undefined && typeof main !== 'boolean') throw new StoreConfigError(`STORES_JSON.${key}.main: want a boolean`)
    views[key] = { key, members, main: main === true, sources: sourceDisplays(key, members, sources) }
  }
  const mains = Object.values(views).filter(v => v.main).map(v => v.key)
  if (mains.length > 1) throw new StoreConfigError(`STORES_JSON: more than one main union view (${mains.join(', ')})`)
  parsed = { raw, stores, views }
  return parsed
}

/** A union view's `sources` (each member's `label` / `icon`), validated: keys are members, values strings. */
function sourceDisplays(key: string, members: readonly string[], raw: unknown): Record<string, SourceDisplay> {
  if (raw === undefined) return {}
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new StoreConfigError(`STORES_JSON.${key}.sources: want an object of member → {label, icon}`)
  const out: Record<string, SourceDisplay> = {}
  for (const [m, d] of Object.entries(raw as Record<string, unknown>)) {
    if (!members.includes(m)) throw new StoreConfigError(`STORES_JSON.${key}.sources: '${m}' is not a member`)
    if (!d || typeof d !== 'object' || Array.isArray(d)) throw new StoreConfigError(`STORES_JSON.${key}.sources.${m}: want an object`)
    const { label, icon, ...rest } = d as Record<string, unknown>
    if (Object.keys(rest).length) throw new StoreConfigError(`STORES_JSON.${key}.sources.${m}: unknown field(s) ${Object.keys(rest).join(', ')} (want label, icon)`)
    if (label !== undefined && (typeof label !== 'string' || !label.trim())) throw new StoreConfigError(`STORES_JSON.${key}.sources.${m}.label: want a nonempty string`)
    if (icon !== undefined && (typeof icon !== 'string' || !ICON_RE.test(icon))) throw new StoreConfigError(`STORES_JSON.${key}.sources.${m}.icon: want an http(s) URL or a site path (/…)`)
    out[m] = { ...(label !== undefined ? { label: label as string } : {}), ...(icon !== undefined ? { icon: icon as string } : {}) }
  }
  return out
}

/** The secondary stores with a page path (`path`), as `{key, path}`: what a page URL's store is read from. */
export const storePaths = (env: Env): { key: string; path: string }[] =>
  Object.entries(secondaryStores(env)).flatMap(([key, c]) => (c.path ? [{ key, path: c.path }] : []))

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
 * Resolve the request's store: a copy of the context whose `env` carries the
 * request (`withReq`) — the primary's env (no `store=`), else that store's
 * overlay; a 400/404/500
 * `Response` for a malformed, unknown or misconfigured store.
 */
export function withStore<C extends { request: Request; env: Env }>(ctx: C): C | Response {
  // `waitUntil` (Pages' EventContext) stays bound to the real context.
  const w = (ctx as { waitUntil?: unknown }).waitUntil
  // The env carries the request (`withReq`: its shared reads are owned by it, and its cancel evicts them).
  const as = (env: Env): C => ({ ...ctx, env: withReq(env, ctx.request.signal), ...(typeof w === 'function' ? { waitUntil: w.bind(ctx) } : {}) })
  const key = requestedStore(ctx.request)
  if (key === null) return as(ctx.env)
  if (!KEY_RE.test(key)) return jsonErr('bad store', 400)
  let stores: Record<string, StoreConfig>
  try {
    stores = secondaryStores(ctx.env)
  } catch (e) {
    return jsonErr((e as Error).message, 500)
  }
  const cfg = Object.hasOwn(stores, key) ? stores[key] : undefined
  if (!cfg) return jsonErr(`unknown store '${key}'`, 404)
  // The request's usage point names the store it read (`_lib/usage.ts`).
  const m = meterOf(ctx)
  if (m) m.store = key
  return as(storeEnv(ctx.env, key, cfg))
}

/** For the primary-only surfaces (the ownership ledger: owners, estate,
 * assignments, assignments, actions): a 404 for any other store, null otherwise. */
export function primaryOnly(ctx: { request: Request }): Response | null {
  const key = requestedStore(ctx.request)
  return key === null ? null : jsonErr(`store '${key}' has no ownership ledger (primary store only)`, 404)
}
