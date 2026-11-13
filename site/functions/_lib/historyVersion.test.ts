import type { R2Conditional, R2PutOptions } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import {
  adoptHistoryVersion, assertHistoryScan, checkedHistoryVersion, historyPointer, historyVersion, historyVersionKey,
  HistoryVersionError, loadHistoryVersion, withHistoryVersion, writeHistoryVersion,
  type HistoryVersion, type HistoryVersionEnv, type HistoryVersionStorage,
} from './historyVersion.js'

const A = '2026-11-01', B = '2026-11-02', C = '2026-11-03', D = '2026-11-04'
const DEV = 'history/environments/dev/CURRENT.json', PROD = 'history/environments/prod/CURRENT.json'
const descriptor = (id = 'v1', extra: Partial<HistoryVersion> = {}): HistoryVersion => ({
  id, interval_gen: `iv-${id}`, static_gen: `sn-${id}`, retained: [A, C], removed: [B], plan_digest: 'a'.repeat(64),
  verified: { interval: true, static: true, inventory: true }, ...extra,
})

function storage() {
  const objects = new Map<string, { body: string; etag: string }>()
  const log: unknown[] = []
  let next = 0
  let beforePut: (() => void) | undefined
  const seed = (key: string, value: unknown, etag = `e${++next}`) => { objects.set(key, { body: JSON.stringify(value), etag }); return etag }
  const bucket: HistoryVersionStorage = {
    async get(key) {
      log.push(['get', key])
      const got = objects.get(key)
      return got ? { etag: got.etag, size: new TextEncoder().encode(got.body).length, json: async <T>() => JSON.parse(got.body) as T } : null
    },
    async put(key: string, body: string, options: R2PutOptions & { onlyIf: R2Conditional }) {
      log.push(['put', key, JSON.parse(body), options])
      beforePut?.()
      const current = objects.get(key), condition = options.onlyIf
      if (condition.etagDoesNotMatch === '*' && current) return null
      if (condition.etagMatches !== undefined && condition.etagMatches !== current?.etag) return null
      const etag = seed(key, JSON.parse(body))
      return { etag }
    },
  }
  const env: HistoryVersionEnv = { INDEX_R2: bucket, HISTORY_CURRENT_KEY: DEV }
  return { bucket, objects, log, seed, env, race(fn: () => void) { beforePut = fn } }
}

const errorShape = async (promise: Promise<unknown>) => {
  try { await promise; throw new Error('Expected HistoryVersionError') } catch (error) {
    if (!(error instanceof HistoryVersionError)) throw error
    return { message: error.message, status: error.status, code: error.code }
  }
}

describe('immutable history descriptors', () => {
  it('creates once with an atomic absence precondition and rejects overwrites', async () => {
    const s = storage(), value = descriptor()
    expect(await writeHistoryVersion(s.bucket, value)).toEqual({ etag: 'e1' })
    expect(await errorShape(writeHistoryVersion(s.bucket, descriptor('v1', { interval_gen: 'replacement' })))).toEqual({
      message: 'History version already exists', status: 409, code: 'history-version-exists',
    })
    expect(await loadHistoryVersion(s.bucket, 'v1')).toEqual(value)
    expect(s.log).toEqual([
      ['put', 'history/versions/v1.json', value, { onlyIf: { etagDoesNotMatch: '*' }, httpMetadata: { contentType: 'application/json' } }],
      ['put', 'history/versions/v1.json', descriptor('v1', { interval_gen: 'replacement' }), { onlyIf: { etagDoesNotMatch: '*' }, httpMetadata: { contentType: 'application/json' } }],
      ['get', 'history/versions/v1.json'],
    ])
  })

  it('requires complete proof and rejects overlapping, unsorted or unsafe descriptors', () => {
    const invalid: unknown[] = [
      { ...descriptor(), verified: { interval: true, static: false, inventory: true } },
      descriptor('v1', { removed: [A] }), descriptor('v1', { retained: [C, A] }),
      descriptor('v1', { interval_gen: '../other' }), descriptor('v1', { plan_digest: '' }),
    ]
    expect(invalid.map(value => {
      try { checkedHistoryVersion(value); return 'accepted' } catch (error) {
        if (!(error instanceof HistoryVersionError)) throw error
        return [error.status, error.code]
      }
    })).toEqual([[409, 'history-not-ready'], [503, 'history-version-error'], [503, 'history-version-error'],
      [503, 'history-version-error'], [503, 'history-version-error']])
    expect(() => historyVersionKey('../../CURRENT')).toThrow('Invalid history version ID')
  })
})

describe('request-scoped history release readers', () => {
  it('captures one pair and roster for a request even after a concurrent adoption', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor('v1'))
    s.seed(historyVersionKey('v2'), descriptor('v2', { retained: [A, C, D] }))
    s.seed(DEV, { version: 'v1' })
    const captured = await withHistoryVersion(s.env)
    s.seed(DEV, { version: 'v2' })
    expect([captured.INTERVAL_STORE_GEN, captured.STATIC_GEN, captured.PATH_STORE, s.env.HISTORY_VERSION_CACHE]).toEqual(['iv-v1', 'sn-v1', 'intervals', undefined])
    expect(await historyVersion(captured)).toEqual(descriptor('v1'))
    expect(await withHistoryVersion(captured)).toBe(captured)
    expect(await historyVersion(s.env)).toEqual(descriptor('v2', { retained: [A, C, D] }))
    expect(s.log).toEqual([
      ['get', DEV], ['get', historyVersionKey('v1')], ['get', DEV], ['get', historyVersionKey('v2')],
    ])
  })

  it('retains normal live behavior without a configured pointer and isolates secondary stores', async () => {
    const s = storage()
    expect(await historyVersion({ INDEX_R2: s.bucket })).toBe(null)
    expect(await historyVersion({ ...s.env, STORE_KEY: 'meta' })).toBe(null)
    expect(await historyPointer({ ...s.env, STORE_KEY: 'meta' })).toBe(null)
    expect(s.log).toEqual([])
  })

  it('freezes pointer absence per request and does not silently fall back from a broken configured pointer', async () => {
    const s = storage(), captured = await withHistoryVersion(s.env)
    s.seed(DEV, { version: 'missing' })
    expect(await historyVersion(captured)).toBe(null)
    expect(await errorShape(historyVersion(s.env))).toEqual({ message: 'History version does not exist', status: 404, code: 'history-version-error' })
    expect(s.log).toEqual([['get', DEV], ['get', DEV], ['get', historyVersionKey('missing')]])
  })

  it('rejects removed dates and dates outside the pinned snapshot before per-scan fallback', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor())
    s.seed(DEV, { version: 'v1' })
    const env = await withHistoryVersion(s.env)
    expect(await assertHistoryScan(env, A)).toBe(undefined)
    expect(await errorShape(assertHistoryScan(env, B))).toEqual({ message: `Scan ${B} was removed from retained history`, status: 410, code: 'history-scan-removed' })
    expect(await errorShape(assertHistoryScan(env, D))).toEqual({ message: `Scan ${D} is not in this pinned history snapshot`, status: 404, code: 'history-scan-not-retained' })
  })
})

describe('environment history adoption CAS', () => {
  it('creates only the selected environment pointer when explicit absence still holds', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor(), 'descriptor')
    s.seed(PROD, { version: 'prod-live' }, 'prod-tag')
    expect(await adoptHistoryVersion(s.env, { version: 'v1', expectedEtag: null, requiredPins: [A] })).toEqual({ version: 'v1', etag: 'e1', previous: null })
    expect([...s.objects.entries()].map(([key, value]) => [key, JSON.parse(value.body)])).toEqual([
      [historyVersionKey('v1'), descriptor()], [PROD, { version: 'prod-live' }], [DEV, { version: 'v1' }],
    ])
    expect(s.log).toEqual([
      ['get', DEV], ['get', historyVersionKey('v1')],
      ['put', DEV, { version: 'v1' }, { onlyIf: { etagDoesNotMatch: '*' }, httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' } }],
    ])
  })

  it('adopts a verified version with the exact observed ETag', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor('v1'))
    s.seed(historyVersionKey('v2'), descriptor('v2', { retained: [A], removed: [B, C] }))
    s.seed(DEV, { version: 'v1' }, 'expected')
    expect(await adoptHistoryVersion(s.env, { version: 'v2', expectedEtag: 'expected', requiredPins: [A] })).toEqual({ version: 'v2', etag: 'e3', previous: 'v1' })
    expect(s.log.at(-1)).toEqual(['put', DEV, { version: 'v2' }, { onlyIf: { etagMatches: 'expected' }, httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' } }])
  })

  it('accepts a verified rollback that restores removed scans and omits scans outside its own snapshot', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor('v1'))
    s.seed(historyVersionKey('v0'), descriptor('v0', { retained: [A, B], removed: [] }))
    s.seed(DEV, { version: 'v1' }, 'expected')
    expect(await adoptHistoryVersion(s.env, { version: 'v0', expectedEtag: 'expected', requiredPins: [A, B] })).toEqual({
      version: 'v0', etag: 'e3', previous: 'v1',
    })
    const env = await withHistoryVersion(s.env)
    expect(await assertHistoryScan(env, B)).toBe(undefined)
    expect(await errorShape(assertHistoryScan(env, C))).toEqual({
      message: `Scan ${C} is not in this pinned history snapshot`, status: 404, code: 'history-scan-not-retained',
    })
    expect(s.log).toEqual([
      ['get', DEV], ['get', historyVersionKey('v0')],
      ['put', DEV, { version: 'v0' }, { onlyIf: { etagMatches: 'expected' }, httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' } }],
      ['get', DEV], ['get', historyVersionKey('v0')],
    ])
  })

  it('refuses stale or wildcard expectations and new operational pins without writing', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor('v1'))
    s.seed(historyVersionKey('v2'), descriptor('v2', { retained: [A, B, C], removed: [] }))
    s.seed(DEV, { version: 'v1' }, 'expected')
    const outcomes = []
    for (const input of [
      { version: 'v2', expectedEtag: null, requiredPins: [] },
      { version: 'v2', expectedEtag: 'stale', requiredPins: [] },
      { version: 'v2', expectedEtag: '*', requiredPins: [] },
      { version: 'v1', expectedEtag: 'expected', requiredPins: [B] },
    ]) outcomes.push(await errorShape(adoptHistoryVersion(s.env, input)))
    expect(outcomes.map(o => [o.status, o.code])).toEqual([
      [409, 'history-pointer-conflict'], [409, 'history-pointer-conflict'], [400, 'history-version-error'],
      [409, 'history-missing-pins'],
    ])
    expect(s.log.filter(item => Array.isArray(item) && item[0] === 'put')).toEqual([])
  })

  it('detects a competing write after validation at the atomic conditional put', async () => {
    const s = storage()
    s.seed(historyVersionKey('v1'), descriptor())
    s.race(() => { s.seed(DEV, { version: 'other' }, 'competing') })
    expect(await errorShape(adoptHistoryVersion(s.env, { version: 'v1', expectedEtag: null, requiredPins: [] }))).toEqual({
      message: 'Environment history pointer changed; review it again', status: 409, code: 'history-pointer-conflict',
    })
    expect(JSON.parse(s.objects.get(DEV)!.body)).toEqual({ version: 'other' })
  })

  it('does not allow adoption to the writer pointers or a secondary store', async () => {
    const s = storage()
    expect(await errorShape(adoptHistoryVersion({ ...s.env, HISTORY_CURRENT_KEY: 'interval-store/CURRENT.json' }, { version: 'v1', expectedEtag: null, requiredPins: [] }))).toEqual({
      message: 'HISTORY_CURRENT_KEY must name history/environments/<environment>/CURRENT.json', status: 503, code: 'history-version-error',
    })
    expect(await errorShape(adoptHistoryVersion({ ...s.env, STORE_KEY: 'meta' }, { version: 'v1', expectedEtag: null, requiredPins: [] }))).toEqual({
      message: 'Environment history adoption is not configured for this store', status: 503, code: 'history-version-error',
    })
    expect(s.log).toEqual([])
  })
})
