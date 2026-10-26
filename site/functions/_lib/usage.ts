/**
 * Usage attribution (specs/usage-metrics.md): one Workers Analytics Engine data point per Functions request,
 * saying which route and which caller made it and what it cost in the units Cloudflare bills (R2 Class B reads
 * and bytes, D1 queries and rows read, KV reads).
 *
 * The root middleware creates a request-scoped `Meter`, runs the request inside it (`metered`) and, after the
 * response, writes the point through `waitUntil` (`writeUsage`): writing never fails or slows a response, and
 * without the `USAGE` binding it is a no-op. The meter is found two ways:
 * - the request's async context (`AsyncLocalStorage`, the `nodejs_als` compatibility flag): what the binding
 *   seams below use, since they see an `env`, not a request;
 * - `ctx.data[METER]` (the Pages context's per-request `data`): what `identify` uses, so the caller is
 *   recorded even where the flag isn't set.
 *
 * The counters fill at the bindings themselves: `instrumentBindings` wraps the env's R2, D1 and KV bindings
 * once per isolate, and each call bumps the current request's meter (none current: nothing counted). The gate
 * (`auth.ts` `identify`) records the caller, `edgeCache.ts` `cacheMatch` the cache tier and `stores.ts`
 * `withStore` the store. Nothing raw goes into a point: the route is a fixed pattern (`routeOf`), the caller a
 * grant's name or note (`callerOf`, emails masked), never a path, query string, email or address.
 */
import type { AnalyticsEngineDataset, D1Database, D1PreparedStatement, D1Result, KVNamespace, R2Bucket, R2ObjectBody } from '@cloudflare/workers-types'

/** How the request proved who it is: a browser session, a share link's session, a Bearer token, nobody, or nobody
 *  with a crawler's user agent. */
export type Via = 'session' | 'grant' | 'token' | 'public' | 'bot'
/** Which response-cache tier answered: the colo cache, the KV tier, neither (computed), or no cache consulted. */
export type CacheTier = 'edge-hit' | 'kv-hit' | 'miss' | 'none'

/** The `ctx.data` key the middleware's meter rides under. */
export const METER = 'usageMeter'

/** One request's attribution and counters. */
export class Meter {
  via: Via | null = null
  caller: string | null = null
  cache: CacheTier = 'none'
  store = 'primary'
  r2Reads = 0
  r2Bytes = 0
  d1Queries = 0
  d1RowsRead = 0
  kvReads = 0
  d1RowsWritten = 0
  constructor(readonly t0: number = Date.now()) {}

  /** What the gate identified (`identify`); the last call wins. */
  identified(via: Via, caller: string): void {
    this.via = via
    this.caller = caller
  }

  /** A response-cache lookup's outcome (`cacheMatch`). A request that consulted the cache more than once (a union
   *  view's members) reports its costliest outcome: any miss is a miss, else any KV hit. */
  cached(tier: Exclude<CacheTier, 'none'>): void {
    const rank: Record<CacheTier, number> = { none: 0, 'edge-hit': 1, 'kv-hit': 2, miss: 3 }
    if (rank[tier] > rank[this.cache]) this.cache = tier
  }
}

/** The routes a point names, in match order: an exact path, or a `/<prefix>/*` covering the prefix itself and
 *  everything under it. Anything else is `page` (an HTML answer to a GET outside `/api/`: the SPA's routes) or
 *  `other`. `usage.test.ts` pins that every Function file maps to one of these. */
export const ROUTES: readonly string[] = [
  '/api/actions', '/api/age-pyramid', '/api/annotations', '/api/assignments', '/api/diff', '/api/digest-image',
  '/api/dups', '/api/estate', '/api/filter-caps', '/api/filter-cover', '/api/filter-scans', '/api/footer-cache',
  '/api/health', '/api/largest', '/api/me', '/api/name-matches', '/api/name-summary', '/api/name-summary-registry', '/api/owner-prices',
  '/api/owners', '/api/path-index', '/api/prefixes', '/api/series', '/api/session-monitor', '/api/static-bench',
  '/api/store', '/api/subtree', '/api/token', '/api/union-scans', '/api/usage', '/api/whoami',
  '/api/plans/prefix-history', '/api/plans/progress', '/api/plans/*',
  '/api/plan-sweep/*', '/api/sweep/*',
  '/api/activity/*', '/api/auth/*', '/api/cost/*', '/api/db/*', '/api/og/*', '/api/scan-runs/*', '/api/session-log/*',
  '/auth/google/*', '/auth/email/*',
  '/data/*', '/og/*', '/v1/files/*', '/v1/objects/*', '/slack/actions',
  '/assignments', '/staged', '/users', '/user/*',
  '/assets/*',
]

/** `pathname`'s route pattern (`ROUTES`), `page` or `other`. */
export function routeOf(pathname: string, method: string, html: boolean): string {
  for (const r of ROUTES) {
    if (r.endsWith('/*') ? pathname === r.slice(0, -2) || pathname.startsWith(r.slice(0, -1)) : pathname === r) return r
  }
  return method === 'GET' && html && !pathname.startsWith('/api/') ? 'page' : 'other'
}

/** Crawlers and link unfurlers, by user agent: what an unidentified request is counted as instead of `public`. */
export const BOT_UA = /bot\b|bot\/|crawler|spider|slurp|facebookexternalhit|embedly|whatsapp|vkshare|preview/i

/** An email-shaped run in a grant's label, masked: a point never carries an address. */
const EMAIL_RE = /[^\s@<>()"',;]+@[^\s@<>()"',;]+\.[^\s@<>()"',;]+/g
/** An Analytics Engine index is at most 96 bytes; a caller is the index, so it's cut to fit. */
export const CALLER_MAX = 96

const fit = (s: string): string => {
  const enc = new TextEncoder()
  let out = s.replace(EMAIL_RE, '<email>').trim()
  while (enc.encode(out).byteLength > CALLER_MAX) out = out.slice(0, -1)
  return out
}

/** A grant-authenticated request's attribution: `token` when it arrived as `Authorization: Bearer` (agent / CLI /
 *  job tokens), else `grant` (a share link's session); the caller is the grant's name, else its note, else the
 *  via itself. */
export function grantCaller(request: Request, grant: { name: string | null; note?: string | null }): { via: Via; caller: string } {
  const via: Via = /^bearer\s/i.test(request.headers.get('authorization') ?? '') ? 'token' : 'grant'
  const label = [grant.name, grant.note].map(s => (s == null ? '' : fit(s))).find(s => s) ?? via
  return { via, caller: label }
}

/** An unidentified request's attribution (the gate recorded nothing): `bot` by user agent, else `public`. */
export function anonymousCaller(request: Request): { via: Via; caller: string } {
  const via: Via = BOT_UA.test(request.headers.get('user-agent') ?? '') ? 'bot' : 'public'
  return { via, caller: via }
}

/** The status class (`2xx` …). */
export const statusClass = (status: number): string => `${Math.floor(status / 100)}xx`

/** What `writeDataPoint` takes (the binding's own type, restated so tests can build one). */
export interface UsagePoint {
  blobs: string[]
  doubles: number[]
  indexes: string[]
}

/** The request's data point (spec `usage-metrics.md` §2's field table): blobs route, method, via, caller, status
 *  class, cache, store, host (`prod` on `prodHost`, else `dev`: a dev stack may share prod's dataset); doubles
 *  count, wall ms, R2 reads, R2 bytes, D1 queries, D1 rows read, KV reads, D1 rows written; indexed by caller. */
export function usagePoint(m: Meter, request: Request, res: Response, wallMs: number, prodHost?: string): UsagePoint {
  const url = new URL(request.url)
  const html = (res.headers.get('content-type') ?? '').startsWith('text/html')
  const who = m.via && m.caller ? { via: m.via, caller: m.caller } : anonymousCaller(request)
  return {
    blobs: [routeOf(url.pathname, request.method, html), request.method, who.via, who.caller, statusClass(res.status), m.cache, m.store, url.host === prodHost ? 'prod' : 'dev'],
    doubles: [1, Math.max(0, Math.round(wallMs)), m.r2Reads, m.r2Bytes, m.d1Queries, m.d1RowsRead, m.kvReads, m.d1RowsWritten],
    indexes: [who.caller],
  }
}

export interface UsageEnv {
  USAGE?: AnalyticsEngineDataset
  PROD_HOST?: string
}

/** Write `point` to the `USAGE` dataset; no binding, nothing. Never throws: a failed write is logged and dropped. */
export function writeUsage(env: UsageEnv, point: UsagePoint): void {
  if (!env.USAGE) return
  try {
    env.USAGE.writeDataPoint(point)
  } catch (e) {
    console.log(`usage point dropped: ${(e as Error).message}`)
  }
}

interface Als<T> {
  run<R>(store: T, f: () => R): R
  getStore(): T | undefined
}

// Computed so the Functions bundler leaves it to the runtime: a deployment without `nodejs_als` still builds, and
// its import fails here instead (no async context: the caller is still recorded through `ctx.data`).
const ALS_MODULE = ['node', 'async_hooks'].join(':')
let alsP: Promise<Als<Meter> | null> | null = null
let als: Als<Meter> | null = null

/** The request-context store, once loaded (null: the runtime has none). */
export const loadAls = (): Promise<Als<Meter> | null> =>
  (alsP ??= import(/* @vite-ignore */ ALS_MODULE)
    .then((m: { AsyncLocalStorage: new () => Als<Meter> }) => (als = new m.AsyncLocalStorage()))
    .catch(() => null))

/** The meter of the request whose async context this is, if any. */
export const currentMeter = (): Meter | undefined => als?.getStore()

/** `ctx`'s meter: the async context's, else the Pages context's `data`. */
export const meterOf = (ctx: object): Meter | undefined =>
  currentMeter() ?? ((ctx as { data?: Record<string, unknown> }).data?.[METER] as Meter | undefined)

/** Run `f` as `m`'s request: the bindings' calls inside it (and in the `waitUntil` work it starts) count on `m`. */
export async function metered<T>(m: Meter, ctx: object, f: () => Promise<T>): Promise<T> {
  const data = (ctx as { data?: Record<string, unknown> }).data
  if (data && typeof data === 'object') data[METER] = m
  const a = await loadAls()
  return a ? a.run(m, f) : f()
}

// --- binding instrumentation ----------------------------------------------------------------------------------

const INSTRUMENTED = Symbol.for('disky.usage.instrumented')
const isFn = (v: unknown): v is (...a: unknown[]) => unknown => typeof v === 'function'
const has = (o: object, ...ks: string[]): boolean => ks.every(k => isFn((o as Record<string, unknown>)[k]))

/** Every other property of a wrapped binding, bound to the real one (a native binding refuses another `this`). */
function forward(target: object, p: string | symbol): unknown {
  const v = Reflect.get(target, p, target)
  return isFn(v) ? v.bind(target) : v
}

/** Bytes an R2 `get` returned: its range's length, else its suffix (capped at the object), else the object. */
function r2BodyBytes(o: R2ObjectBody): number {
  const r = (o as { range?: { offset?: number; length?: number; suffix?: number } }).range
  if (r?.length != null) return r.length
  if (r?.suffix != null) return Math.min(r.suffix, o.size)
  return o.size
}

/** `r2` with every `get` / `head` (Class B) counted on the current meter, and a `get`'s bytes. */
export function meteredR2(r2: R2Bucket): R2Bucket {
  return new Proxy(r2, {
    get(target, p) {
      if (p === 'get') {
        return async (...args: Parameters<R2Bucket['get']>) => {
          const o = await target.get(...args)
          const m = currentMeter()
          if (m) {
            m.r2Reads++
            if (o && 'size' in o) m.r2Bytes += r2BodyBytes(o as R2ObjectBody)
          }
          return o
        }
      }
      if (p === 'head') {
        return async (...args: Parameters<R2Bucket['head']>) => {
          const o = await target.head(...args)
          const m = currentMeter()
          if (m) m.r2Reads++
          return o
        }
      }
      return forward(target, p)
    },
  })
}

const realStmt = new WeakMap<object, D1PreparedStatement>()

function rowsMeta(r: unknown, k: 'rows_read' | 'rows_written'): number {
  const n = (r as { meta?: Record<string, unknown> } | null)?.meta?.[k]
  return typeof n === 'number' ? n : 0
}

/** One result's rows read and written onto `m` (D1 bills both from the result's `meta`). */
function chargeRows(m: Meter, r: unknown): void {
  m.d1RowsRead += rowsMeta(r, 'rows_read')
  m.d1RowsWritten += rowsMeta(r, 'rows_written')
}

/** A prepared statement whose executions count on the current meter (`batch` unwraps it back to the real one). */
function meteredStmt(s: D1PreparedStatement): D1PreparedStatement {
  const p = new Proxy(s, {
    get(target, k) {
      if (k === 'bind') return (...a: unknown[]) => meteredStmt(target.bind(...a))
      if (k === 'first' || k === 'raw') {
        return async (...a: unknown[]) => {
          const r = await (target[k] as (...x: unknown[]) => Promise<unknown>)(...a)
          const m = currentMeter()
          if (m) m.d1Queries++
          return r
        }
      }
      if (k === 'all' || k === 'run') {
        return async () => {
          const r = await target[k]()
          const m = currentMeter()
          if (m) { m.d1Queries++; chargeRows(m, r) }
          return r
        }
      }
      return forward(target, k)
    },
  })
  realStmt.set(p, s)
  return p
}

/** `db` (or a session of it) with each statement it runs counted: queries, and rows read / written where the result's
 *  `meta` says (`all` / `run` / `batch`; `first` / `raw` carry no meta). */
export function meteredD1<D extends Pick<D1Database, 'prepare' | 'batch'>>(db: D): D {
  return new Proxy(db, {
    get(target, k) {
      if (k === 'prepare') return (sql: string) => meteredStmt(target.prepare(sql))
      if (k === 'batch') {
        return async (stmts: D1PreparedStatement[]) => {
          const rs = await target.batch(stmts.map(s => realStmt.get(s) ?? s)) as D1Result[]
          const m = currentMeter()
          if (m) {
            m.d1Queries += stmts.length
            for (const r of rs) chargeRows(m, r)
          }
          return rs
        }
      }
      if (k === 'withSession') {
        return (...a: unknown[]) => meteredD1((target as unknown as { withSession(...x: unknown[]): D }).withSession(...a))
      }
      return forward(target, k)
    },
  })
}

/** `kv` with each read counted (a bulk `get` of N keys counts N, as KV bills it). */
export function meteredKV(kv: KVNamespace): KVNamespace {
  return new Proxy(kv, {
    get(target, k) {
      if (k === 'get' || k === 'getWithMetadata') {
        return async (key: unknown, ...a: unknown[]) => {
          const r = await (target[k] as (...x: unknown[]) => Promise<unknown>)(key, ...a)
          const m = currentMeter()
          if (m) m.kvReads += Array.isArray(key) ? key.length : 1
          return r
        }
      }
      return forward(target, k)
    },
  })
}

/** Wrap `env`'s R2, D1 and KV bindings (recognized by shape) in place, once: the env object is the isolate's, shared
 *  by its requests, and each wrapped call charges the meter of the request it runs in. Only with an async context
 *  (`loadAls`); without one no call could be attributed, so the bindings stay as they are. */
export function instrumentBindings(env: object): void {
  const e = env as Record<string | symbol, unknown>
  if (!als || e[INSTRUMENTED] || Object.isFrozen(e)) return
  Object.defineProperty(e, INSTRUMENTED, { value: true })
  for (const [k, v] of Object.entries(e)) {
    if (!v || typeof v !== 'object') continue
    if (has(v, 'writeDataPoint')) continue
    if (has(v, 'prepare', 'batch', 'exec')) e[k] = meteredD1(v as D1Database)
    else if (has(v, 'get', 'head', 'put', 'list', 'createMultipartUpload')) e[k] = meteredR2(v as R2Bucket)
    else if (has(v, 'get', 'getWithMetadata', 'put', 'list')) e[k] = meteredKV(v as KVNamespace)
  }
}
