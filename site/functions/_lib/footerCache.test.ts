import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { EVICT_GRACE_S, evictFooters, fill, FILL_STALE_S, footerCacheDrain, type FooterRow, ROW_FIXED_BYTES } from './footerCache'
import { openIndex, readRects, type Rect } from './index'
import { type Sqlite, sqliteD1 } from './testD1'
import { type D1Variant, readJson, seedGeneration } from './testStore'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The footer LRU (specs/done/footer-lru.md) over the v2 fixture generation (`fixtures/v2/`: `path` and
// `bysize` sorts, 4 tier groups each, one per cold-footer group), retired from D1 with its
// `.groups.parquet` beside it — what a miss serves from and fills from.

const v2Files = {
  path: { parquet: 'v2/path-index.parquet', groups: 'v2/path-index.groups.json', footer: 'v2/path-index.groups.parquet' },
  bysize: { parquet: 'v2/path-index-bysize.parquet', groups: 'v2/path-index-bysize.groups.json', footer: 'v2/path-index-bysize.groups.parquet' },
}
const T0 = Date.UTC(2026, 9, 14, 12) // 2026-10-14T12:00Z
const DAY0 = Math.floor(T0 / 86_400_000)
const S0 = T0 / 1000
const small: Rect = { dLo: 3, dHi: 1e9, pLo: 'bk/small/', pHi: 'bk/small0' }

let v2: Record<string, D1Variant>
/** The D1 rows `index-sync` would write for a variant (the fixture's, without the footer-only `b_min`). */
const syncRows = (variant: string) => v2[variant].rows.map(({ b_min: _, ...r }) => r)
const bytesOf = (variant: string) => syncRows(variant).reduce((s, r) => s + r.p_min.length + r.p_max.length + (r.u_min?.length ?? 0) + (r.u_max?.length ?? 0) + r.rg_json.length + ROW_FIXED_BYTES, 0)
const ROW_COLS = 'rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json'
const d1Rows = (raw: Sqlite, date: string, variant: string) =>
  raw.prepare(`SELECT ${ROW_COLS} FROM index_row_groups WHERE date = ? AND variant = ? ORDER BY rg`).all(date, variant).map(r => ({ ...r }))
const entries = (raw: Sqlite) =>
  raw.prepare('SELECT date, variant, gen, state, rows, bytes, started_ts, filled_ts, last_access_day FROM footer_cache ORDER BY date, variant, gen').all().map(r => ({ ...r }))
const envOf = (db: D1Database): Env => ({ DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' }) as Env
/** A generation retired from D1, its cold footers beside it (`date` unique per test: open handles are memoized). */
const seedRetired = (raw: Sqlite, date: string) =>
  seedGeneration(raw, { date, gen: 'g1', dir: `cw-l2/${date}/index/g1`, variants: v2, files: v2Files, retired: ['path', 'bysize'] })
/** A generation with its rows in D1, as `index-sync` leaves it. */
const seedSynced = (raw: Sqlite, date: string) =>
  seedGeneration(raw, { date, gen: 'g1', dir: `cw-l2/${date}/index/g1`, variants: v2, files: v2Files })
const insertRows = (raw: Sqlite, date: string, variant: string, rows: ReturnType<typeof syncRows>) => {
  for (const r of rows) {
    raw.prepare(`INSERT INTO index_row_groups (date, variant, gen, ${ROW_COLS}) VALUES (?, ?, 'g1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(date, variant, r.rg, r.d_min, r.d_max, r.p_min, r.p_max, r.b_max, r.u_min, r.u_max, r.row_start, r.row_end, r.rg_json)
  }
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  v2 = await readJson<Record<string, D1Variant>>('v2/d1.json')
})
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(T0)
})
afterEach(async () => {
  await footerCacheDrain()
  vi.useRealTimers()
})

describe('a miss', () => {
  it('serves from the cold footer, and the drained fill makes the entry hot: D1 serves it after the handle expires', async () => {
    const { db, raw } = await sqliteD1()
    seedRetired(raw, 'miss')
    const env = envOf(db)
    const cold = await openIndex(env, 'miss', 'path')
    expect(cold.mode).toBe('pq')
    const want = (await readRects(cold, [small], () => 1)).map(r => r.path)
    expect(want).toEqual(['bk/small/s0', 'bk/small/s1', 'bk/small/s2'])
    // Nothing is written on the request's path.
    expect(d1Rows(raw, 'miss', 'path')).toEqual([])
    expect(entries(raw)).toEqual([])

    await footerCacheDrain()
    expect(d1Rows(raw, 'miss', 'path')).toEqual(syncRows('path'))
    expect(entries(raw)).toEqual([{ date: 'miss', variant: 'path', gen: 'g1', state: 'hot', rows: 4, bytes: bytesOf('path'), started_ts: S0, filled_ts: S0, last_access_day: DAY0 }])

    vi.setSystemTime(T0 + 61_000)
    const hot = await openIndex(env, 'miss', 'path')
    expect(hot.mode).toBe('d1')
    expect((await readRects(hot, [small], () => 1)).map(r => r.path)).toEqual(want)
    // Already stamped today: no access write queued.
    const before = entries(raw)
    await footerCacheDrain()
    expect(entries(raw)).toEqual(before)
  })

  it('a secondary store fills its namespaced variant, `store` set', async () => {
    const { db, raw } = await sqliteD1()
    seedGeneration(raw, { date: 'sec', gen: 'g1', dir: 'meta-l2/sec/index/g1', variants: v2, files: v2Files, retired: ['path', 'bysize'], store: 'meta' })
    const env = { ...envOf(db), STORE_KEY: 'meta' } as Env
    expect((await openIndex(env, 'sec', 'bysize')).mode).toBe('pq')
    await footerCacheDrain()
    expect(raw.prepare("SELECT DISTINCT store, variant FROM index_row_groups WHERE date = 'sec'").all().map(r => ({ ...r }))).toEqual([{ store: 'meta', variant: 'meta:bysize' }])
    expect(d1Rows(raw, 'sec', 'meta:bysize')).toEqual(syncRows('bysize'))
    expect(entries(raw).map(e => [e.variant, e.state])).toEqual([['meta:bysize', 'hot']])
  })
})

describe('a partial fill', () => {
  it('is not trusted: a fresh `filling` entry with some rows serves cold and queues nothing', async () => {
    const { db, raw } = await sqliteD1()
    seedRetired(raw, 'part')
    insertRows(raw, 'part', 'path', syncRows('path').slice(0, 2))
    raw.prepare("INSERT INTO footer_cache (date, variant, gen, state, started_ts) VALUES ('part', 'path', 'g1', 'filling', ?)").run(S0 - 60)
    const h = await openIndex(envOf(db), 'part', 'path')
    expect(h.mode).toBe('pq')
    expect((await readRects(h, [small], () => 1)).map(r => r.path)).toEqual(['bk/small/s0', 'bk/small/s1', 'bk/small/s2'])
    await footerCacheDrain()
    expect(d1Rows(raw, 'part', 'path')).toEqual(syncRows('path').slice(0, 2))
    expect(entries(raw).map(e => [e.state, e.started_ts])).toEqual([['filling', S0 - 60]])
  })

  it('a stale claim is taken over by the next miss, which completes the fill', async () => {
    const { db, raw } = await sqliteD1()
    seedRetired(raw, 'stale')
    insertRows(raw, 'stale', 'path', syncRows('path').slice(0, 2))
    raw.prepare("INSERT INTO footer_cache (date, variant, gen, state, started_ts) VALUES ('stale', 'path', 'g1', 'filling', ?)").run(S0 - FILL_STALE_S - 1)
    expect((await openIndex(envOf(db), 'stale', 'path')).mode).toBe('pq')
    await footerCacheDrain()
    expect(d1Rows(raw, 'stale', 'path')).toEqual(syncRows('path'))
    expect(entries(raw)).toEqual([{ date: 'stale', variant: 'path', gen: 'g1', state: 'hot', rows: 4, bytes: bytesOf('path'), started_ts: S0, filled_ts: S0, last_access_day: DAY0 }])
  })

  it('stops landing rows once its claim is gone (an eviction mid-fill), and never turns hot', async () => {
    const { db, raw } = await sqliteD1()
    const rows = syncRows('path') as FooterRow[]
    const k = { date: 'mid', variant: 'path', gen: 'g1' }
    async function* groups(): AsyncIterable<FooterRow[]> {
      yield rows.slice(0, 2)
      raw.prepare("UPDATE footer_cache SET state = 'evicting' WHERE date = 'mid'").run()
      yield rows.slice(2)
    }
    expect(await fill(envOf(db), k, groups)).toBe('lost')
    expect(d1Rows(raw, 'mid', 'path')).toEqual(syncRows('path').slice(0, 2))
    expect(entries(raw).map(e => e.state)).toEqual(['evicting'])
  })

  it('two fills at once: one claims, the other is `busy`; an evicting entry is never claimed', async () => {
    const { db, raw } = await sqliteD1()
    const rows = syncRows('path') as FooterRow[]
    const k = { date: 'two', variant: 'path', gen: 'g1' }
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    async function* slow(): AsyncIterable<FooterRow[]> { await gate; yield rows }
    async function* fast(): AsyncIterable<FooterRow[]> { yield rows }
    const first = fill(envOf(db), k, slow)
    expect(await fill(envOf(db), k, fast)).toBe('busy')
    release()
    expect(await first).toBe('filled')
    expect(d1Rows(raw, 'two', 'path')).toEqual(syncRows('path'))
    raw.prepare("UPDATE footer_cache SET state = 'evicting', started_ts = 0").run()
    expect(await fill(envOf(db), k, fast)).toBe('busy')
  })
})

describe('access', () => {
  it('is written once per day: not again the same day, again the next', async () => {
    const { db, raw } = await sqliteD1()
    seedSynced(raw, 'acc')
    raw.prepare("INSERT INTO footer_cache (date, variant, gen, state, rows, bytes, started_ts, filled_ts, last_access_day) VALUES ('acc', 'path', 'g1', 'hot', 4, 1, 0, 0, ?)").run(DAY0 - 3)
    const env = envOf(db)
    const writes: string[] = []
    const prepare = db.prepare.bind(db)
    ;(db as unknown as { prepare: (sql: string) => unknown }).prepare = (sql: string) => {
      if (/^UPDATE footer_cache SET last_access_day/.test(sql)) writes.push(sql)
      return prepare(sql)
    }
    const day = () => raw.prepare("SELECT last_access_day AS d FROM footer_cache WHERE date = 'acc'").all()[0].d
    expect((await openIndex(env, 'acc', 'path')).mode).toBe('d1')
    expect(day()).toBe(DAY0 - 3)
    await footerCacheDrain()
    expect([writes.length, day()]).toEqual([1, DAY0])
    // Later the same day (the handle re-opened): nothing queued.
    vi.setSystemTime(T0 + 3_600_000)
    expect((await openIndex(env, 'acc', 'path')).mode).toBe('d1')
    await footerCacheDrain()
    expect([writes.length, day()]).toEqual([1, DAY0])
    // The next day: one more.
    vi.setSystemTime(T0 + 86_400_000)
    expect((await openIndex(env, 'acc', 'path')).mode).toBe('d1')
    await footerCacheDrain()
    expect([writes.length, day()]).toEqual([2, DAY0 + 1])
  })

  it('an untracked synced generation is trusted as before, and stamps nothing', async () => {
    const { db, raw } = await sqliteD1()
    seedSynced(raw, 'unt')
    expect((await openIndex(envOf(db), 'unt', 'path')).mode).toBe('d1')
    await footerCacheDrain()
    expect(entries(raw)).toEqual([])
  })
})

describe('eviction', () => {
  /** Hot entries `[date, rows, bytes, last_access_day]`, each with `rows` fake row groups in D1. */
  async function seeded(es: [string, number, number, number][]) {
    const { db, raw } = await sqliteD1()
    for (const [date, n, bytes, d] of es) {
      raw.prepare("INSERT INTO footer_cache (date, variant, gen, state, rows, bytes, started_ts, filled_ts, last_access_day) VALUES (?, 'path', 'g1', 'hot', ?, ?, 0, 0, ?)").run(date, n, bytes, d)
      for (let rg = 0; rg < n; rg++) raw.prepare("INSERT INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) VALUES (?, 'path', 'g1', ?, 0, 0, '', '', 0, 0, 0, '[]')").run(date, rg)
    }
    return { db, raw }
  }
  const counts = (raw: Sqlite) => raw.prepare('SELECT date, COUNT(*) AS n FROM index_row_groups GROUP BY date ORDER BY date').all().map(r => [r.date, r.n])
  const states = (raw: Sqlite) => entries(raw).map(e => [e.date, e.state])

  it('demotes the least recently read past the row budget (ties: the older scan), deletes them after the grace', async () => {
    const { db, raw } = await seeded([['s1', 3, 30, 10], ['s2', 2, 20, 12], ['s3', 4, 40, 10], ['s4', 1, 10, 15]])
    const budget = { maxRows: 5, maxBytes: null }
    const r = await evictFooters(db, budget, { nowMs: T0 })
    expect(r).toEqual({
      budget,
      hot: { entries: 4, rows: 10, bytes: 100 },
      demoted: [
        { date: 's1', variant: 'path', gen: 'g1', rows: 3, bytes: 30, lastAccessDay: 10 },
        { date: 's3', variant: 'path', gen: 'g1', rows: 4, bytes: 40, lastAccessDay: 10 },
      ],
      deleted: [],
    })
    expect(states(raw)).toEqual([['s1', 'evicting'], ['s2', 'hot'], ['s3', 'evicting'], ['s4', 'hot']])
    expect(counts(raw)).toEqual([['s1', 3], ['s2', 2], ['s3', 4], ['s4', 1]])
    // Within the grace: nothing more.
    const again = await evictFooters(db, budget, { nowMs: T0 + (EVICT_GRACE_S - 1) * 1000 })
    expect([again.demoted, again.deleted]).toEqual([[], []])
    // Past it: rows, then entries.
    const done = await evictFooters(db, budget, { nowMs: T0 + EVICT_GRACE_S * 1000 })
    expect([done.hot, done.demoted, done.deleted.map(e => e.date)]).toEqual([{ entries: 2, rows: 3, bytes: 30 }, [], ['s1', 's3']])
    expect(states(raw)).toEqual([['s2', 'hot'], ['s4', 'hot']])
    expect(counts(raw)).toEqual([['s2', 2], ['s4', 1]])
  })

  it('a byte budget evicts too; within budget and a dry run write nothing', async () => {
    const { db, raw } = await seeded([['s1', 1, 50, 10], ['s2', 1, 50, 11], ['s3', 1, 50, 12]])
    const dry = await evictFooters(db, { maxRows: 100, maxBytes: 60 }, { nowMs: T0, dryRun: true })
    expect(dry.demoted.map(e => e.date)).toEqual(['s1', 's2'])
    expect(states(raw)).toEqual([['s1', 'hot'], ['s2', 'hot'], ['s3', 'hot']])
    const fits = await evictFooters(db, { maxRows: 3, maxBytes: 150 }, { nowMs: T0 })
    expect([fits.demoted, fits.deleted]).toEqual([[], []])
    const now = await evictFooters(db, { maxRows: 100, maxBytes: 60 }, { nowMs: T0, graceS: 0 })
    expect([now.demoted.map(e => e.date), now.deleted.map(e => e.date)]).toEqual([['s1', 's2'], ['s1', 's2']])
    expect(states(raw)).toEqual([['s3', 'hot']])
    expect(counts(raw)).toEqual([['s3', 1]])
  })

  it('an evicting entry serves cold and is not refilled', async () => {
    const { db, raw } = await sqliteD1()
    seedSynced(raw, 'ev')
    raw.prepare("INSERT INTO footer_cache (date, variant, gen, state, rows, bytes, started_ts, last_access_day) VALUES ('ev', 'path', 'g1', 'evicting', 4, 1, ?, 0)").run(S0)
    expect((await openIndex(envOf(db), 'ev', 'path')).mode).toBe('pq')
    await footerCacheDrain()
    expect(entries(raw).map(e => [e.state, e.started_ts])).toEqual([['evicting', S0]])
  })
})

describe('without the `footer_cache` table', () => {
  it('reads exactly as before: rows → D1, none → cold; nothing filled, no error', async () => {
    const { db, raw } = await sqliteD1()
    raw.exec('DROP TABLE footer_cache')
    seedRetired(raw, 'old')
    seedSynced(raw, 'old2')
    const sqls: string[] = []
    const prepare = db.prepare.bind(db)
    ;(db as unknown as { prepare: (sql: string) => unknown }).prepare = (sql: string) => { sqls.push(sql); return prepare(sql) }
    const env = envOf(db)
    expect((await openIndex(env, 'old', 'path')).mode).toBe('pq')
    expect((await openIndex(env, 'old2', 'path')).mode).toBe('d1')
    await footerCacheDrain()
    expect(d1Rows(raw, 'old', 'path')).toEqual([])
    // The probe failed once; the table's absence is remembered, so the second open asks only the pre-LRU query.
    const probes = sqls.filter(s => s.includes('footer_cache')).length
    const legacy = sqls.filter(s => s === 'SELECT 1 AS x FROM index_row_groups WHERE date = ? AND variant = ? AND gen = ? LIMIT 1').length
    expect([probes, legacy]).toEqual([1, 2])
  })
})
