/** A pinned history release controls both index families and the date roster before any legacy fallback. */
import { describe, expect, it, vi } from 'vitest'
import type { Env } from './auth.js'
import { HistoryVersionError, historyVersionKey, type HistoryVersion } from './historyVersion.js'
import { intervalsOn, ivGen, openIndex, pathScans, staticCurrentGen, withPathStore, withStaticGen } from './index.js'
import { storeEnv } from './stores.js'
import { sqliteD1 } from './testD1.js'

const A = '2026-11-01', B = '2026-11-02', C = '2026-11-03', D = '2026-11-04'
const POINTER = 'history/environments/history-reader-test/CURRENT.json'
const descriptor = (id = 'v1', retained = [A, C]): HistoryVersion => ({
  id, interval_gen: `history-reader-iv-${id}`, static_gen: `history-reader-static-${id}`,
  retained, removed: [B], plan_digest: 'a'.repeat(64), verified: { interval: true, static: true, inventory: true },
})

function r2() {
  const records = new Map<string, unknown>([
    [POINTER, { version: 'v1' }], [historyVersionKey('v1'), descriptor()],
    [historyVersionKey('v2'), descriptor('v2', [A, C, D])],
  ])
  const reads: string[] = []
  let afterRead: ((key: string) => void) | undefined
  const bucket = {
    async get(key: string) {
      reads.push(key)
      if (!records.has(key)) {
        if (key === POINTER) { afterRead?.(key); return null }
        throw new Error(`Unexpected non-history R2 read: ${key}`)
      }
      const body = JSON.stringify(records.get(key))
      afterRead?.(key)
      return { size: new TextEncoder().encode(body).length, etag: `etag-${key}`, json: async () => JSON.parse(body) }
    },
  } as unknown as R2Bucket
  return { records, reads, bucket, onRead(fn: (key: string) => void) { afterRead = fn } }
}

async function fixture() {
  const { db, raw } = await sqliteD1(), blobs = r2()
  raw.exec(`INSERT INTO index_schema (store,date,variant,version,schema_json,gen,dir) VALUES
    ('primary','${B}','path',2,'[]','old-scan','old/${B}'),
    ('primary','${D}','path',2,'[]','new-scan','new/${D}'),
    ('meta','${B}','meta:path',2,'[]','meta-scan','meta/${B}'),
    ('meta','${D}','meta:path',2,'[]','meta-new','meta/${D}');`)
  const prepare = vi.spyOn(db, 'prepare')
  const env = {
    DB: db, INDEX_R2: blobs.bucket, HISTORY_CURRENT_KEY: POINTER,
    PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'old-live-iv', STATIC_GEN: 'old-live-static',
  } as Env
  return { ...blobs, env, prepare }
}

const at = (env: Env, query = `date=${A}`) => withPathStore({ request: new Request(`https://history.test/api/subtree?${query}`), env })
const failure = async (promise: Promise<unknown>) => {
  try { await promise; throw new Error('Expected a history refusal') } catch (error) {
    if (!(error instanceof HistoryVersionError)) throw error
    return { status: error.status, code: error.code, message: error.message }
  }
}

describe('history release integration with path-store readers', () => {
  it('resolves both generations once per request and ps=scan cannot disable the immutable release', async () => {
    const f = await fixture(), ctx = await at(f.env, `date=${A}&ps=scan`)
    f.records.set(POINTER, { version: 'v2' })
    expect([
      ctx.env.INTERVAL_STORE_GEN, ctx.env.STATIC_GEN, ctx.env.PATH_STORE, intervalsOn(ctx.env),
      await ivGen(ctx.env), await staticCurrentGen(ctx.env),
    ]).toEqual(['history-reader-iv-v1', 'history-reader-static-v1', 'intervals', true,
      'history-reader-iv-v1', 'history-reader-static-v1'])
    expect(await withPathStore(ctx)).toBe(ctx)
    expect(await pathScans(ctx.env, true)).toEqual({ results: [{ date: A }, { date: C }] })
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
    expect(f.prepare.mock.calls).toEqual([])
    const next = await at(f.env)
    expect([next.env.INTERVAL_STORE_GEN, next.env.STATIC_GEN]).toEqual(['history-reader-iv-v2', 'history-reader-static-v2'])
    expect([f.env.INTERVAL_STORE_GEN, f.env.STATIC_GEN, f.env.HISTORY_VERSION_CACHE]).toEqual(['old-live-iv', 'old-live-static', undefined])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1'), POINTER, historyVersionKey('v2')])
  })

  it('the retained roster overrides removed and future D1 dates without querying D1', async () => {
    const f = await fixture(), ctx = await at(f.env)
    expect([await pathScans(ctx.env, true), await pathScans(ctx.env, false)]).toEqual([
      { results: [{ date: A }, { date: C }] }, { results: [{ date: A }, { date: C }] },
    ])
    expect(f.prepare.mock.calls).toEqual([])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
  })

  it('captures pointer absence through live fallback when adoption races with the first read', async () => {
    const f = await fixture()
    f.records.delete(POINTER)
    f.onRead(key => { if (key === POINTER) f.records.set(POINTER, { version: 'v1' }) })
    const ctx = await at(f.env)
    expect([ctx.env.INTERVAL_STORE_GEN, ctx.env.STATIC_GEN, ctx.env.PATH_STORE, ctx.env.HISTORY_VERSION_CACHE?.version]).toEqual([
      'old-live-iv', 'old-live-static', 'intervals', null,
    ])
    expect(f.reads).toEqual([POINTER])
    expect(f.prepare.mock.calls).toEqual([])
  })

  it('static-only readers honor the release before a legacy STATIC_GEN configuration pin', async () => {
    const f = await fixture(), env = await withStaticGen(f.env)
    expect([env.STATIC_GEN, env.INTERVAL_STORE_GEN, env.HISTORY_VERSION_CACHE?.version?.id]).toEqual([
      'history-reader-static-v1', 'history-reader-iv-v1', 'v1',
    ])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
    expect(f.prepare.mock.calls).toEqual([])
  })

  it('openIndex refuses a removed date before D1 or per-scan lookup even when per-scan mode was requested', async () => {
    const f = await fixture()
    expect(await failure(openIndex({ ...f.env, PATH_STORE: undefined }, B))).toEqual({
      status: 410, code: 'history-scan-removed', message: `Scan ${B} was removed from retained history`,
    })
    expect(f.prepare.mock.calls).toEqual([])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
  })

  it.each(['date', 'from', 'to'])('validates the %s parameter before a ps=scan parity escape', async parameter => {
    const f = await fixture()
    expect(await failure(at(f.env, `${parameter}=${B}&ps=scan`))).toEqual({
      status: 410, code: 'history-scan-removed', message: `Scan ${B} was removed from retained history`,
    })
    expect(f.prepare.mock.calls).toEqual([])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
  })

  it('a later D1 date does not leak into a pinned release via openIndex fallback', async () => {
    const f = await fixture(), ctx = await at(f.env)
    expect(await failure(openIndex(ctx.env, D))).toEqual({
      status: 404, code: 'history-scan-not-retained', message: `Scan ${D} is not in this pinned history snapshot`,
    })
    expect(f.prepare.mock.calls).toEqual([])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
  })

  it('a secondary store ignores the primary release, including a cached primary snapshot inherited by its overlay', async () => {
    const f = await fixture(), captured = await at(f.env)
    const meta = storeEnv(captured.env, 'meta', {
      vars: { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'meta-iv', STATIC_GEN: 'meta-static' },
    })
    const ctx = await at(meta, `date=${B}&store=meta&ps=scan`)
    expect([ctx.env.STORE_KEY, ctx.env.INTERVAL_STORE_GEN, ctx.env.STATIC_GEN, ctx.env.PATH_STORE]).toEqual([
      'meta', 'meta-iv', 'meta-static', undefined,
    ])
    expect(await pathScans(ctx.env, true)).toEqual({ results: [{ date: B }, { date: D }] })
    expect(f.prepare.mock.calls).toEqual([[
      'SELECT DISTINCT date FROM index_schema WHERE store = ? AND variant = ?',
    ]])
    expect(f.reads).toEqual([POINTER, historyVersionKey('v1')])
  })
})
