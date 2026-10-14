/** The scan an API request names — the Functions side of `src/scanSlug.ts`'s
 * resolver, against the store's index (`index_schema`'s `path` pointers).
 *
 *   ?date=<scan id>   (also `from=`, `to=`) exactly that scan
 *   ?d=<slug>         the latest indexed scan the slug names (`261009` a day,
 *                     `26100912` an hour, `2610091236` a minute — a date-only
 *                     scan sharing its day is keyed by its start, `meta.started`
 *                     (`2610090430`), else its midnight, `2610090000`, which
 *                     stays an alias; legacy and ISO spellings too)
 *
 * A scan id or slug naming no indexed scan is a 404 `{error: "no scan matches
 * d=26100912"}` — never the nearest or the latest scan instead. */
import { decodeScan, isScanId, latestScan } from '../../src/scanSlug.js'
import { type Env, json } from './auth.js'
import { scanTimes } from './scanTimes.js'
import { d1Variant, isPrimary, storeKey } from './stores.js'

/** The latest indexed scan matching `prefix` (exact: only `prefix` itself).
 * Without a D1 there is no index to ask: an exact id passes as given, a slug
 * resolves to nothing. A slug resolves among its day's indexed scans (every
 * decoded prefix names at least a day, and a date-only scan only ever matches
 * on its own day) by the shared resolver, with the starts of that day's
 * date-only scans (`scanTimes`, read from their metas only when the day holds
 * more than one scan). */
export async function indexedScan(env: Env, prefix: string, exact: boolean): Promise<string | null> {
  if (!env.DB) return exact ? prefix : null
  // A decoded prefix is `[0-9T-]` only: no LIKE metacharacters to escape.
  const where = isPrimary(env) ? "variant = 'path'" : 'store = ? AND variant = ?'
  const scope = isPrimary(env) ? [] : [storeKey(env), d1Variant(env, 'path')]
  if (exact) {
    const r = await env.DB.prepare(`SELECT MAX(date) AS d FROM index_schema WHERE ${where} AND date = ?`).bind(...scope, prefix).first<{ d: string | null }>()
    return r?.d ?? null
  }
  const r = await env.DB.prepare(`SELECT DISTINCT date FROM index_schema WHERE ${where} AND date LIKE ?`).bind(...scope, `${prefix.slice(0, 10)}%`).all<{ date: string }>()
  const day = r.results.map(x => x.date)
  return latestScan(prefix, day, await scanTimes(env, day))
}

/** The 404 a miss answers: `{error: "no scan matches <key>=<value>"}`. */
export const noScan = (key: string, value: string): Response => json({ error: `no scan matches ${key}=${value}` }, 404)

/** The scan `key` names: `date`/`from`/`to` take an exact scan id (absent
 * `date` falls back to a `d=<slug>`); `d` a slug. A missing or malformed
 * exact id is a 400; a miss (an unindexed id, a slug matching nothing or
 * naming no scan) a 404. */
export async function scanArg(env: Env, sp: URLSearchParams, key = 'date'): Promise<string | Response> {
  const raw = sp.get(key)
  if (raw === null && key === 'date' && sp.has('d')) return scanArg(env, sp, 'd')
  if (key === 'd') {
    const prefix = raw ? decodeScan(raw) : undefined
    return (prefix && await indexedScan(env, prefix, false)) || noScan('d', raw ?? '')
  }
  if (!raw || !isScanId(raw)) return json({ error: `${key}=YYYY-MM-DD[THHMM] required` }, 400)
  return (await indexedScan(env, raw, true)) ?? noScan(key, raw)
}
