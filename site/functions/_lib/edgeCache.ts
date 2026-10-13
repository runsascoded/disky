/** Two-tier cache for the auth-gated, per-scan-immutable JSON endpoints
 * (`/api/subtree`, `/api/diff`): the colo cache (Workers Cache API) in front
 * of a global KV tier.
 *
 * The client copy is `private` (browsers may keep it; shared proxies must
 * not — the data is behind sign-in). But the Workers Cache API refuses to
 * store a response whose Cache-Control says not to share it: `cache.put`
 * reports that as a 413, which the endpoints never checked, so every diff and
 * subtree was recomputed for every viewer (a repeated 7-day root diff took
 * 21 s, 2026-09-15). The stored copy therefore carries `public, s-maxage`;
 * a hit is rewritten back to `private` on the way out. The scope gate
 * (`requireScope`) runs before `match`, so a hit still only reaches an
 * authorized viewer.
 *
 * The colo cache is per data-center, so the daily job's warm-up
 * (`dt-cloud warm-cache`, run in us-central1) would reach one colo; KV is
 * global, so a warmed key is a hit for the first viewer anywhere. A KV hit
 * back-fills the colo cache. Values are small JSON (≤ ~350 KB); keys are the
 * SHA-256 of the cache key URL (KV keys are capped at 512 bytes). */

import { PRIMARY_STORE } from './stores.js'

const TTL = 86400
const KV_TTL = 30 * 86400
// The browser's copy is short-lived: the answer for a (scan pair, path) is
// immutable per scan, but a deploy that changes a reader rule is not, and a
// day-long `max-age` had the page (and every probe from it) replaying the
// pre-deploy diff while every other window was fresh (2026-09-17). The edge
// tiers keep the long TTL behind the versioned key.
const PRIVATE = 'private, max-age=300'
const JSON_HDR = { 'content-type': 'application/json; charset=utf-8' }

export interface CacheEnv { CACHE_KV?: KVNamespace; CACHE_NS?: string; PROD_HOST?: string }

// Bumped whenever a cached endpoint's answer for the same inputs changes
// (a reader rule, a diff walk rule): a stale entry lives a day in the colo
// cache and a month in KV, and neither knows a deploy happened. 2026-09-17:
// one-sided diff nodes expand. 3 (2026-10-07): owner pools fold the live
// ledger, objects included (a preview of the bytes-only fold wrote pool
// answers with unconserved object counts into KV). 4: pool and user-lens
// object counts exact from the manifest's per-user objects (`uo`).
export const CACHE_V = '4'

/** The deployment environment a response-cache key belongs to: `''` is prod's (keys exactly as before the
 * tag existed — no one-time flush), anything else a separate keyspace. A Pages preview (the dev stack)
 * binds the same `CACHE_KV` namespace as its prod and shares its zone's colo cache, so without the tag an
 * answer computed by dev's reader (new code, dev-only flags) was served on prod for up
 * to 30 days. `CACHE_NS`, when set, is the tag (`""` = prod's keys); else the request host, except
 * `PROD_HOST` itself — prod declares `PROD_HOST`, its preview env doesn't inherit `[vars]`, and a request to
 * the dev host (or a `*.pages.dev` alias) is never at the prod host. */
export function cacheEnvTag(env: CacheEnv, request: Request): string {
  if (env.CACHE_NS != null) return env.CACHE_NS
  const host = new URL(request.url).host
  return host === env.PROD_HOST ? '' : host
}

/** The cache key for `parts` of endpoint `kind`, in deployment environment `tag` (`cacheEnvTag`). A
 * non-prod tag adds a `~<tag>/` segment, which the KV key (a hash of the URL) inherits. A secondary store's
 * keys gain an `@<store>/` segment (specs/multi-store.md), so the same path in two stores can't collide;
 * the primary's (`store` omitted or `'primary'`) are exactly what they were before stores existed — no
 * one-time cache miss. */
export function cacheKeyFor(kind: string, tag: string, parts: string, store?: string): Request {
  const e = tag ? `~${encodeURIComponent(tag)}/` : ''
  const s = store && store !== PRIMARY_STORE ? `@${store}/` : ''
  return new Request(`https://${kind}.cache/v${CACHE_V}/${e}${s}${parts}`)
}

const colo = () => (caches as unknown as { default: Cache }).default

async function kvKey(key: Request): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key.url))
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')
}

const publicRes = (body: string) => new Response(body, { headers: { ...JSON_HDR, 'cache-control': `public, s-maxage=${TTL}` } })
const clientRes = (body: string, tier: string) => new Response(body, { headers: { ...JSON_HDR, 'cache-control': PRIVATE, 'x-cache': tier } })

export async function cacheMatch(env: CacheEnv, key: Request): Promise<Response | null> {
  const hit = await colo().match(key)
  if (hit) {
    const res = new Response(hit.body, hit)
    // A partial answer's browser copy lives no longer than its edge copy.
    const partial = /ttl=(\d+)/.exec(hit.headers.get('x-cache-partial') ?? '')
    res.headers.set('cache-control', partial ? `private, max-age=${partial[1]}` : PRIVATE)
    res.headers.set('x-cache', 'hit')
    return res
  }
  if (!env.CACHE_KV) return null
  const body = await env.CACHE_KV.get(await kvKey(key), 'text')
  if (body == null) return null
  await colo().put(key, publicRes(body))
  return clientRes(body, 'kv')
}

/** How long a budget-cut answer (phase 2 stopped by its time budget: `interiors.late`) is kept in the colo
 * cache, seconds. Its totals and match counts are exact; only the drawn interiors are partial, and a
 * recompute pays the same budget again (gcs 10-09 `nemotron`: every retry of the root diff re-ran both
 * sides' 900 ms phase 2 and drew nothing more). Short, and colo-only (never KV): a later miss may complete. */
export const PARTIAL_TTL = 120

/** What `cacheStore` keeps: `true` both tiers for a day / a month; `false` nothing (served only);
 * `{ partialTtl }` the colo tier only, for that many seconds (a budget-cut answer, `PARTIAL_TTL`). */
export type Keep = boolean | { partialTtl: number }

/** What a filter view or diff keeps: a phase 2 cut by its time budget (`interiors.late`) briefly, else all. */
export const keepFor = (interiors?: { late?: number }): Keep => interiors?.late ? { partialTtl: PARTIAL_TTL } : true

/** Store `body` (already-serialized JSON) per `keep` and return the client response. With ``waitUntil``
 * (the Pages `EventContext`'s) the writes run after the response is sent — a KV put is hundreds of ms the
 * viewer needn't wait for; without it they complete first. A partial answer's client copy says so
 * (`x-cache-partial: ttl=<s>`), on a hit too (the stored copy carries the header). */
export async function cacheStore(env: CacheEnv, key: Request, body: string, headers: Record<string, string> = {}, waitUntil?: (p: Promise<unknown>) => void, keep: Keep = true): Promise<Response> {
  // `false`: an answer that must not be kept at all is served only.
  if (keep === false) {
    const res = clientRes(body, 'miss')
    for (const [k, v] of Object.entries(headers)) res.headers.set(k, v)
    res.headers.set('x-cache-store', 'skipped')
    return res
  }
  const partial = typeof keep === 'object' ? keep.partialTtl : null
  const puts = async () => {
    if (partial != null) {
      await colo().put(key, new Response(body, { headers: { ...JSON_HDR, 'cache-control': `public, s-maxage=${partial}`, 'x-cache-partial': `ttl=${partial}` } }))
      return
    }
    const ps: Promise<unknown>[] = [colo().put(key, publicRes(body))]
    if (env.CACHE_KV) ps.push(env.CACHE_KV.put(await kvKey(key), body, { expirationTtl: KV_TTL }))
    await Promise.all(ps)
  }
  let stored = partial != null ? `partial;ttl=${partial}` : 'deferred'
  if (waitUntil) waitUntil(puts().catch(() => undefined))
  else { const t0 = performance.now(); await puts(); stored = `${partial != null ? `partial;ttl=${partial};` : ''}awaited;dur=${Math.round(performance.now() - t0)}` }
  const res = clientRes(body, 'miss')
  for (const [k, v] of Object.entries(headers)) res.headers.set(k, v)
  res.headers.set('x-cache-store', stored)
  if (partial != null) {
    res.headers.set('x-cache-partial', `ttl=${partial}`)
    res.headers.set('cache-control', `private, max-age=${partial}`)
  }
  return res
}

/** A background full run's phase-2 time budget, ms (`upgradePartial`): what the viewer-facing 1.5 s (0.9 s a
 *  diff side) cut short gets ~7× more, after the response is sent. A Pages Function's `waitUntil` may run
 *  30 s past the response, and phase 2 stops decoding at its budget, so a diff (both sides at once, then its
 *  walk) stays well inside. */
export const UPGRADE_PHASE2_MS = 10_000

/** Per isolate: keys with a background full run in flight (one each: concurrent partial hits don't
 *  stampede), and keys whose run was cut short too — not retried until the partial they'd replace expires
 *  (`PARTIAL_TTL`), so a heavy key doesn't re-run every hit. */
const upgrading = new Set<string>()
const gaveUp = new Map<string, number>()

/** What `upgradePartial` did: `scheduled`, or why not. */
export type Upgrade = 'scheduled' | 'no-waituntil' | 'in-flight' | 'gave-up'

/** After serving a partial answer (budget-cut: the miss that produced it, or a partial hit), compute the full
 *  one in the background (`waitUntil`) with `compute` (the endpoint's build at `UPGRADE_PHASE2_MS`). A whole
 *  answer replaces the colo entry, at the normal TTL (and goes to KV, as any whole answer does); one cut
 *  short again leaves the partial as it is. `done` (tests) settles with the run. */
export function upgradePartial(
  env: CacheEnv,
  key: Request,
  compute: () => Promise<{ body: string; keep: Keep }>,
  waitUntil: ((p: Promise<unknown>) => void) | undefined,
  now = Date.now(),
): Upgrade {
  if (!waitUntil) return 'no-waituntil'
  const k = key.url
  if (upgrading.has(k)) return 'in-flight'
  const until = gaveUp.get(k)
  if (until != null) {
    if (now < until) return 'gave-up'
    gaveUp.delete(k)
  }
  upgrading.add(k)
  waitUntil((async () => {
    try {
      const { body, keep } = await compute()
      if (keep === true) await cacheStore(env, key, body)
      else gaveUp.set(k, now + PARTIAL_TTL * 1000)
    } catch {
      gaveUp.set(k, now + PARTIAL_TTL * 1000)
    } finally {
      upgrading.delete(k)
    }
  })())
  return 'scheduled'
}

/** Tests: forget the isolate's upgrade state. */
export const resetUpgrades = (): void => { upgrading.clear(); gaveUp.clear() }

/** Whether a cached response is a partial answer (`x-cache-partial`). */
export const isPartial = (r: Response): boolean => r.headers.has('x-cache-partial')

/** A `Trace` sink plus its `Server-Timing` rendering (`fetch;dur=812,…`;
 * counts ride as `dur` too — DevTools shows them the same way). */
export function serverTiming(): { trace: (name: string, ms: number, desc?: string) => void; time: <T>(name: string, p: Promise<T>) => Promise<T>; header: () => string } {
  const t: Record<string, number> = {}
  // A phase's `desc`: the distinct labels its calls carried (the index sort
  // that answered a read — `bysize`, `path`, …), in first-seen order, so a
  // request that read both says `bysize+path`.
  const d: Record<string, string[]> = {}
  const t0 = performance.now()
  const trace = (name: string, ms: number, desc?: string) => {
    t[name] = (t[name] ?? 0) + ms
    if (desc && !(d[name] ??= []).includes(desc)) d[name].push(desc)
  }
  return {
    trace,
    time: async (name, p) => { const s = performance.now(); try { return await p } finally { trace(name, performance.now() - s) } },
    header: () => [...Object.entries(t), ['total', performance.now() - t0] as [string, number]]
      .map(([k, v]) => `${k};dur=${Math.round(v)}${d[k] ? `;desc="${d[k].join('+')}"` : ''}`).join(', '),
  }
}
