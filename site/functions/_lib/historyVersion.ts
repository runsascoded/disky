/** Immutable, verified history releases and an environment's conditional reader pointer.
 *
 * Only this environment's HISTORY_CURRENT_KEY changes on adoption. The two writer CURRENT.json objects, append
 * manifests and successor chains are untouched. A release is a pinned snapshot: later writer appends are separate.
 * Resolve once per request with withHistoryVersion; never keep a mutable request snapshot at module scope.
 *
 * R2 conditional puts return null on a failed precondition (Workers API, Conditional operations):
 * https://developers.cloudflare.com/r2/api/workers/workers-api-reference/#conditional-operations
 */
import type { R2Conditional, R2PutOptions } from '@cloudflare/workers-types'
import { isScanId } from '../../src/scanSlug.js'

export interface HistoryVersion extends Record<string, unknown> {
  id: string
  store?: 'primary'
  interval_gen: string
  static_gen: string
  retained: string[]
  /** Scans excluded by this version; choosing another verified version can restore them. */
  removed: string[]
  plan_digest: string
  verified: { interval: true; static: true; inventory: true; [key: string]: unknown }
}

/** Narrow, structurally compatible surface of the official R2 binding, also used by precise test doubles. */
export interface HistoryVersionStorage {
  get(key: string): Promise<{ size: number; etag: string; json<T>(): Promise<T> } | null>
  put(key: string, value: string, options: R2PutOptions & { onlyIf: R2Conditional }): Promise<{ etag: string } | null>
}

export interface HistoryPointer { version: string; etag: string }
export interface HistoryVersionSnapshot {
  key: string
  store: 'primary'
  pointer: HistoryPointer | null
  version: HistoryVersion | null
}
export interface HistoryVersionEnv {
  INDEX_R2?: HistoryVersionStorage
  /** Separate configured keys for dev and prod. Unset means ordinary live-reader behavior. */
  HISTORY_CURRENT_KEY?: string
  STORE_KEY?: string
  HISTORY_VERSION_CACHE?: HistoryVersionSnapshot
  INTERVAL_STORE_GEN?: string
  STATIC_GEN?: string
  PATH_STORE?: string
}

export class HistoryVersionError extends Error {
  constructor(message: string, readonly status = 400, readonly code = 'history-version-error') { super(message) }
}

export const HISTORY_VERSION_PREFIX = 'history/versions/'
export const HISTORY_VERSION_BYTES = 1 << 20
const ID = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const validId = (v: unknown): v is string => typeof v === 'string' && ID.test(v) && !v.includes('..')
const scanIds = (v: unknown): v is string[] => Array.isArray(v) && v.length <= 100_000 && v.every(isScanId)
  && new Set(v).size === v.length && v.every((s, i) => !i || v[i - 1] < s)
const primary = (env: HistoryVersionEnv): boolean => (env.STORE_KEY ?? 'primary') === 'primary'

export function historyVersionKey(id: string): string {
  if (!validId(id)) throw new HistoryVersionError('Invalid history version ID')
  return `${HISTORY_VERSION_PREFIX}${id}.json`
}

/** No arbitrary R2 key from a request: the key is deployment configuration and lives outside writer pointers. */
function pointerKey(env: HistoryVersionEnv): string | null {
  if (!primary(env)) return null
  const key = env.HISTORY_CURRENT_KEY?.trim()
  if (!key) return null
  if (!/^history\/environments\/[a-zA-Z0-9][a-zA-Z0-9._-]*\/CURRENT\.json$/.test(key) || key.includes('..')) {
    throw new HistoryVersionError('HISTORY_CURRENT_KEY must name history/environments/<environment>/CURRENT.json', 503)
  }
  return key
}

function bucketOf(env: HistoryVersionEnv): HistoryVersionStorage {
  if (!env.INDEX_R2) throw new HistoryVersionError('History versions require INDEX_R2', 503)
  return env.INDEX_R2
}

export function checkedHistoryVersion(value: unknown): HistoryVersion {
  if (!record(value) || !validId(value.id) || !validId(value.interval_gen) || !validId(value.static_gen)
    || (value.store !== undefined && value.store !== 'primary') || !scanIds(value.retained) || !value.retained.length
    || !scanIds(value.removed) || typeof value.plan_digest !== 'string' || !/^[a-f0-9]{64}$/.test(value.plan_digest)) {
    throw new HistoryVersionError('Invalid history version descriptor', 503)
  }
  const retained = new Set(value.retained)
  if (value.removed.some(s => retained.has(s))) throw new HistoryVersionError('History retained and removed scans overlap', 503)
  const verified = value.verified
  if (!record(verified) || verified.interval !== true || verified.static !== true || verified.inventory !== true) {
    throw new HistoryVersionError('History version is not verified and ready', 409, 'history-not-ready')
  }
  return {
    ...value, id: value.id, interval_gen: value.interval_gen, static_gen: value.static_gen,
    ...(value.store === 'primary' ? { store: 'primary' as const } : {}),
    retained: [...value.retained], removed: [...value.removed], plan_digest: value.plan_digest,
    verified: { ...verified, interval: true, static: true, inventory: true },
  }
}

async function readJson(bucket: HistoryVersionStorage, key: string): Promise<{ value: unknown; etag: string } | null> {
  const got = await bucket.get(key)
  if (!got) return null
  if (got.size > HISTORY_VERSION_BYTES) throw new HistoryVersionError('History version metadata exceeds the size limit', 503)
  let value: unknown
  try { value = await got.json<unknown>() } catch { throw new HistoryVersionError('Invalid history version JSON', 503) }
  return { value, etag: got.etag }
}

export async function loadHistoryVersion(bucket: HistoryVersionStorage, id: string): Promise<HistoryVersion> {
  const got = await readJson(bucket, historyVersionKey(id))
  if (!got) throw new HistoryVersionError('History version does not exist', 404)
  const version = checkedHistoryVersion(got.value)
  if (version.id !== id) throw new HistoryVersionError('History version descriptor ID does not match its key', 503)
  return version
}

/** Builder-side create only: once verified, a descriptor can never be replaced through this protocol. */
export async function writeHistoryVersion(bucket: HistoryVersionStorage, value: HistoryVersion): Promise<{ etag: string }> {
  const version = checkedHistoryVersion(value), body = JSON.stringify(version)
  if (new TextEncoder().encode(body).length > HISTORY_VERSION_BYTES) throw new HistoryVersionError('History version metadata exceeds the size limit', 413)
  const got = await bucket.put(historyVersionKey(version.id), body, {
    onlyIf: { etagDoesNotMatch: '*' }, httpMetadata: { contentType: 'application/json' },
  })
  if (!got) throw new HistoryVersionError('History version already exists', 409, 'history-version-exists')
  return { etag: got.etag }
}

/** Fresh admin read, intentionally independent of any request's already captured reader snapshot. */
export async function historyPointer(env: HistoryVersionEnv): Promise<HistoryPointer | null> {
  const key = pointerKey(env)
  if (!key) return null
  const got = await readJson(bucketOf(env), key)
  if (!got) return null
  if (!record(got.value) || !validId(got.value.version)) throw new HistoryVersionError('Invalid environment history pointer', 503)
  return { version: got.value.version, etag: got.etag }
}

function cached(env: HistoryVersionEnv, key: string): HistoryVersionSnapshot | undefined {
  const snapshot = env.HISTORY_VERSION_CACHE
  return snapshot?.key === key && snapshot.store === 'primary' ? snapshot : undefined
}

export async function historyVersion(env: HistoryVersionEnv): Promise<HistoryVersion | null> {
  const key = pointerKey(env)
  if (!key) return null
  const snapshot = cached(env, key)
  if (snapshot) return snapshot.version
  const pointer = await historyPointer(env)
  return pointer ? loadHistoryVersion(bucketOf(env), pointer.version) : null
}

/** Call after store selection and once per request. A secondary store cannot inherit the primary release. */
export async function withHistoryVersion<E extends HistoryVersionEnv>(env: E): Promise<E> {
  const key = pointerKey(env)
  if (!key) return env
  if (cached(env, key)) return env
  const pointer = await historyPointer(env)
  const version = pointer ? await loadHistoryVersion(bucketOf(env), pointer.version) : null
  const snapshot: HistoryVersionSnapshot = { key, store: 'primary', pointer, version }
  return { ...env, HISTORY_VERSION_CACHE: snapshot,
    ...(version ? { INTERVAL_STORE_GEN: version.interval_gen, STATIC_GEN: version.static_gen, PATH_STORE: 'intervals' } : {}) }
}

/** Guard all date readers before per-scan fallback: a release answers only its explicitly retained observations. */
export async function assertHistoryScan(env: HistoryVersionEnv, scan: string): Promise<void> {
  const version = await historyVersion(env)
  if (!version) return
  if (version.removed.includes(scan)) throw new HistoryVersionError(`Scan ${scan} was removed from retained history`, 410, 'history-scan-removed')
  if (!version.retained.includes(scan)) throw new HistoryVersionError(`Scan ${scan} is not in this pinned history snapshot`, 404, 'history-scan-not-retained')
}

export interface HistoryAdoption { version: string; expectedEtag: string | null; requiredPins: readonly string[] }

/** Admin-gated callers supply fresh operational pins. Any verified version can be selected, including a rollback;
 * its own retained roster defines availability. No missing/unconditional/wildcard expected write is accepted. */
export async function adoptHistoryVersion(env: HistoryVersionEnv, input: HistoryAdoption): Promise<{ version: string; etag: string; previous: string | null }> {
  const key = pointerKey(env)
  if (!key) throw new HistoryVersionError('Environment history adoption is not configured for this store', 503)
  if (input.expectedEtag !== null && (typeof input.expectedEtag !== 'string' || !input.expectedEtag || input.expectedEtag.includes('*'))) {
    throw new HistoryVersionError('expectedEtag must be the exact observed ETag or explicit null for absence')
  }
  if (!Array.isArray(input.requiredPins) || !input.requiredPins.every(isScanId)) throw new HistoryVersionError('requiredPins must be complete scan IDs')
  const bucket = bucketOf(env), pointer = await historyPointer(env)
  if ((pointer?.etag ?? null) !== input.expectedEtag) throw new HistoryVersionError('Environment history pointer changed; review it again', 409, 'history-pointer-conflict')
  const version = await loadHistoryVersion(bucket, input.version)
  const missing = input.requiredPins.filter(s => !version.retained.includes(s))
  if (missing.length) throw new HistoryVersionError(`History version omits required pinned scans: ${[...new Set(missing)].sort().join(', ')}`, 409, 'history-missing-pins')
  const got = await bucket.put(key, JSON.stringify({ version: version.id }), {
    onlyIf: input.expectedEtag === null ? { etagDoesNotMatch: '*' } : { etagMatches: input.expectedEtag },
    httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' },
  })
  if (!got) throw new HistoryVersionError('Environment history pointer changed; review it again', 409, 'history-pointer-conflict')
  return { version: version.id, etag: got.etag, previous: pointer?.version ?? null }
}
