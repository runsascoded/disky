/** Histograms encoded as change-interval cells by dt-cloud age-store. */
import type { Env } from './auth.js'
import { ivGen, openInterval, readRows } from './index.js'
import { storeKey } from './stores.js'

// DuckDB url_encode uses RFC 3986's unreserved set, stricter than encodeURIComponent.
const cellPath = (path: string): string => encodeURIComponent(path).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)

/** Null only when this store has no age history for the scan. A held but broken run throws, preserving the
 * interval reader's 503 behavior; an empty histogram stays empty without falling back to a retired file. */
export async function readAgePoint(env: Env, date: string, tier: string, path: string): Promise<Record<string, unknown>[] | null> {
  if (!env.AGE_STORE_PREFIX || !env.INDEX_R2) return null
  const own: Env = {
    ...env, STORE_KEY: `${storeKey(env)}-age`, PATH_STORE: 'intervals',
    INTERVAL_STORE_PREFIX: `${env.AGE_STORE_PREFIX.replace(/\/$/, '')}/interval-store`, INTERVAL_STORE_GEN: undefined,
  }
  const gen = await ivGen(own)
  if (!gen) return null
  own.INTERVAL_STORE_GEN = gen
  const handle = await openInterval(own, date, 'path')
  if (!handle) return null
  const prefix = `${tier}/p=${cellPath(path)}/`
  const rows = await readRows(handle, 3, 3, prefix, `${prefix}\uffff`)
  return rows.map(r => ({ path, depth: path ? path.split('/').length : 0, binstart: Number(r.path.slice(prefix.length)), b: r.size, o: r.n_files }))
}
