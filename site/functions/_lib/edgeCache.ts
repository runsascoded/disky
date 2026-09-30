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

export interface CacheEnv { CACHE_KV?: KVNamespace }

// Bumped whenever a cached endpoint's answer for the same inputs changes
// (a reader rule, a diff walk rule): a stale entry lives a day in the colo
// cache and a month in KV, and neither knows a deploy happened. 2026-09-17:
// one-sided diff nodes expand.
export const CACHE_V = '2'

/** The cache key for `parts` under namespace `ns`. A secondary store's keys
 * gain an `@<store>/` segment (specs/multi-store.md), so the same path in two
 * stores can't collide; the primary's (`store` omitted or `'primary'`) are
 * exactly what they were before stores existed — no one-time cache miss. */
export function cacheKeyFor(ns: string, parts: string, store?: string): Request {
  const s = store && store !== PRIMARY_STORE ? `@${store}/` : ''
  return new Request(`https://${ns}.cache/v${CACHE_V}/${s}${parts}`)
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
    res.headers.set('cache-control', PRIVATE)
    res.headers.set('x-cache', 'hit')
    return res
  }
  if (!env.CACHE_KV) return null
  const body = await env.CACHE_KV.get(await kvKey(key), 'text')
  if (body == null) return null
  await colo().put(key, publicRes(body))
  return clientRes(body, 'kv')
}

/** Store `body` (already-serialized JSON) in both tiers; return the client response. */
/** Store `body` (already-serialized JSON) in both tiers and return the client
 * response. With ``waitUntil`` (the Pages `EventContext`'s) the writes run
 * after the response is sent — a KV put is hundreds of ms the viewer needn't
 * wait for; without it they complete first. */
export async function cacheStore(env: CacheEnv, key: Request, body: string, headers: Record<string, string> = {}, waitUntil?: (p: Promise<unknown>) => void): Promise<Response> {
  const puts = async () => {
    const ps: Promise<unknown>[] = [colo().put(key, publicRes(body))]
    if (env.CACHE_KV) ps.push(env.CACHE_KV.put(await kvKey(key), body, { expirationTtl: KV_TTL }))
    await Promise.all(ps)
  }
  let stored = 'deferred'
  if (waitUntil) waitUntil(puts().catch(() => undefined))
  else { const t0 = performance.now(); await puts(); stored = `awaited;dur=${Math.round(performance.now() - t0)}` }
  const res = clientRes(body, 'miss')
  for (const [k, v] of Object.entries(headers)) res.headers.set(k, v)
  res.headers.set('x-cache-store', stored)
  return res
}

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
