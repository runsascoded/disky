/**
 * The capture hosts a deployment serves (specs/hosts-registry.md): each D1 `hosts` row is a store,
 * shaped like the NAS's (the primary's bucket and credentials, its own `snapshots/<store>/` subdir,
 * `file:///` paths), so a connected Mac shows up without a deploy.
 *
 * `loadHosts` reads the table once per isolate per `TTL_MS` (the middleware awaits it before any
 * Function runs); `cachedHosts` hands that read to the synchronous store resolution (`stores.ts`
 * `secondaryStores`). A D1 without the table (not migrated) reads as no hosts.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type Env, baseScope, identify } from './auth.js'
import type { StoreConfig } from './stores.js'

export interface HostRow {
  id: string
  store: string
  label: string
  home: string | null
  root: string | null
  mark: string | null
  revoked_at: string | null
}

/** A host as the client sees it (`/api/hosts`, the page's `__DISKY_HOSTS__`). */
export interface HostInfo {
  id: string
  store: string
  label: string
  home?: string
  root?: string
  mark?: string
  revoked?: true
}

export const TTL_MS = 60_000

/** Store keys a host can't take: the store mounts at `/<key>`, so an app route's first segment is out. */
export const RESERVED_KEYS: ReadonlySet<string> = new Set([
  'primary', 'api', 'auth', 'data', 'v1', 'og', 'slack', 'assets', 'icons',
  'admin', 'dev', 'files', 'health', 'hot', 'mark', 'marks', 'meta', 'names', 'privacy', 'runs', 'scans', 'signin',
  'staged', 'sweep', 'users', 'user', 'profile',
])

const cache = new WeakMap<object, { at: number; rows: HostRow[] }>()

/** The `hosts` rows, through the per-isolate cache; [] without a DB or a table. */
export async function loadHosts(env: Pick<Env, 'DB'>, now: number = Date.now()): Promise<HostRow[]> {
  const db = env.DB
  if (!db) return []
  const hit = cache.get(db)
  if (hit && now - hit.at < TTL_MS) return hit.rows
  const rows = await readHosts(db)
  cache.set(db, { at: now, rows })
  return rows
}

/** What the last `loadHosts` read for this env's DB (none yet = []). */
export const cachedHosts = (env: Pick<Env, 'DB'>): HostRow[] => (env.DB && cache.get(env.DB)?.rows) || []

/** Drop the cache for this DB (after a write, so the writing isolate sees it at once). */
export const forgetHosts = (env: Pick<Env, 'DB'>): void => { if (env.DB) cache.delete(env.DB) }

async function readHosts(db: D1Database): Promise<HostRow[]> {
  try {
    const r = await db.prepare('SELECT id, store, label, home, root, mark, revoked_at FROM hosts ORDER BY created_at, id').all<HostRow>()
    return r.results ?? []
  } catch (e) {
    if (/no such table: hosts/.test((e as Error).message)) return []
    throw e
  }
}

/** A non-primary host's store config: the NAS's shape, on the primary's bucket and credentials. */
export function hostStoreConfig(env: Env, row: HostRow): StoreConfig {
  const vars: StoreConfig['vars'] = {
    ROOT_LABEL: row.label,
    SNAPSHOTS_SUBDIR: row.store,
    STORE_SCHEME: 'file:///',
    STORE_BUCKETS: '*',
    ...(env.STORE_BUCKET ? { STORE_BUCKET: env.STORE_BUCKET } : {}),
    ...(env.STORE_REGION ? { STORE_REGION: env.STORE_REGION } : {}),
  }
  return {
    vars,
    secrets: { STORE_ENDPOINT: 'STORE_ENDPOINT', STORE_ACCESS_KEY_ID: 'STORE_ACCESS_KEY_ID', STORE_SECRET_ACCESS_KEY: 'STORE_SECRET_ACCESS_KEY' },
  }
}

export const hostInfo = (r: HostRow): HostInfo => ({
  id: r.id,
  store: r.store,
  label: r.label,
  ...(r.home ? { home: r.home } : {}),
  ...(r.root ? { root: r.root } : {}),
  ...(r.mark ? { mark: r.mark } : {}),
  ...(r.revoked_at ? { revoked: true as const } : {}),
})

/** The page's stamp: `<script>window.__DISKY_HOSTS__=[…]</script>`, `<` escaped so a label can't close the tag. */
export const hostsScript = (hosts: HostInfo[]): string =>
  `<script>window.__DISKY_HOSTS__=${JSON.stringify(hosts).replace(/</g, '\\u003c')}</script>`

/** What a page stamps (`window.__DISKY_HOSTS__`, read by `src/stores.ts` before the first render, so a host enrolled
 *  since the build has its routes at boot): the non-primary hosts, for a member's session only (host labels are
 *  private: a guest's or a signed-out page is as built); null = stamp nothing. */
export async function pageHosts(ctx: { request: Request; env: Env }): Promise<HostInfo[] | null> {
  const hosts = cachedHosts(ctx.env).filter(h => h.store !== 'primary')
  if (!hosts.length) return null
  const id = await identify(ctx as never)
  const member = id?.via === 'session' && (id.scopes.includes(baseScope(ctx.env)) || id.scopes.includes('*'))
  return member ? hosts.map(hostInfo) : null
}
