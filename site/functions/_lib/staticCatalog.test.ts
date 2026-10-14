import { parquetMetadata, parquetReadObjects } from 'hyparquet'
import { beforeAll, describe, expect, it } from 'vitest'
import { HOT_SCOPE } from '../../src/hotModel.js'
import { nameResultForRegistry, parseName, parseNameRegistry, STATIC_CATALOG_SOURCE, STATIC_SOURCE } from '../../src/nameModel.js'
import { answerKey, scanIds, staticRegistryBody, staticSummary, type Store } from './nameSummaryStatic.js'
import { catalogAnswer, catalogGroups, catalogIndex, type CatalogMeta, StaticCatalog } from './staticCatalog.js'
import { type Blobs, StaticNames } from './staticNames.js'
import { compressors } from './zstd.js'
import { fixture } from './testStore.js'

/** `gen.py`'s `TERMS` (`members.json`: each one's catalog membership, its range rows or −1 when short). */
const KEYS = ['shards.json', 'scans.json', 'expected.json', 'members.json', 'sx/s0000.parquet', 'sx/s0001.parquet', 'catalog/cells.parquet', 'catalog/index.parquet', 'catalog/meta.json']
const DATES = ['2026-08-01', '2026-09-01', '2026-10-01']
const BUCKETS = ['bkt-a', 'bkt-b', 'bkt-c', 'bkt-d', 'bkt-e', 'bkt-f']
const STATIC_GEN = '2026-10-08c'
const ENV = { STORE_BUCKETS: [...BUCKETS].reverse().join(','), STORE: 'gcs', STATIC_GEN }
const held = new Map<string, ArrayBuffer>()
let expected: Record<string, Record<string, Record<string, [number, number]>>>
let members: Record<string, number | null>
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of KEYS) { const b = fs.readFileSync(fixture(`static-names/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  const text = (k: string) => JSON.parse(new TextDecoder().decode(held.get(k)))
  expected = text('expected.json')
  members = text('members.json')
})
const bytes = (key: string) => { const b = held.get(key); if (!b) throw new Error(`no fixture ${key}`); return b }
/** The fixture as `Blobs`, recording each read (`key@offset+length`). */
function files(log: string[] = []): Blobs {
  return {
    async range(key, offset, length) { const b = bytes(key); const end = length == null ? b.byteLength : offset + length; log.push(`${key}@${offset}+${end - offset}`); return b.slice(offset, end) },
    async suffix(key, n) { const b = bytes(key), size = b.byteLength; log.push(`${key}@-${n}`); return { buf: b.slice(Math.max(0, size - n)), size } },
    async json(key) { log.push(key); return JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
function fixtureStore(log: string[] = []): Store {
  const blobs = files(log)
  return { names: new StaticNames(blobs), catalog: new StaticCatalog(blobs), scans: scanIds(blobs), clock: async () => Date.now() }
}
const num = (answers: Record<string, Record<string, [bigint, bigint]>>) =>
  Object.fromEntries(Object.entries(answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).filter(([, [b, o]]) => b !== 0n || o !== 0n).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))
const terms = () => Object.keys(expected)
const TERMS = ['foo', 'foo.', 'foo-2', 'oof', 'fooz', 'qux', 'mmm', 'andú', 'abcdefghijklmnopqrstuvwxyz-0001', 'bkt', 'zzz', 'f', 'fo', 'o', '-', 'zq', 'bkt-a', 'abcdefghijklmnopqrstuvwxyz-000', 'abcdefghijklmnopqrstuvwxyz-0002.parquet', 'parquet', '.parquet', 'quet']
const params = (q: Record<string, string>) => new URLSearchParams(q)

describe('static catalog', () => {
  it('decodes index.parquet into the lookup arrays: each group\'s exact first/last `q`, span and column chunks', async () => {
    const meta = JSON.parse(new TextDecoder().decode(bytes('catalog/meta.json'))) as CatalogMeta
    const idx = await catalogIndex(bytes('catalog/index.parquet'), meta)
    const cells = bytes('catalog/cells.parquet'), md = parquetMetadata(cells)
    const rows = await parquetReadObjects({ file: cells, compressors }) as { q: string }[]
    const start = (c: (typeof md.row_groups)[number]['columns'][number]) => Number(c.meta_data!.dictionary_page_offset ?? c.meta_data!.data_page_offset)
    let at = 0
    const first: string[] = [], last: string[] = []
    for (const rg of md.row_groups) { const n = Number(rg.num_rows); first.push(rows[at].q); last.push(rows[at + n - 1].q); at += n }
    expect(idx).toEqual({
      size: cells.byteLength, qMin: first, qMax: last,
      offset: md.row_groups.map(rg => Math.min(...rg.columns.map(start))),
      length: md.row_groups.map(rg => Math.max(...rg.columns.map(c => start(c) + Number(c.meta_data!.total_compressed_size))) - Math.min(...rg.columns.map(start))),
      rows: md.row_groups.map(rg => Number(rg.num_rows)),
      chunks: md.row_groups.flatMap(rg => rg.columns.flatMap(c => [Number(c.meta_data!.data_page_offset), Number(c.meta_data!.total_compressed_size), Number(c.meta_data!.dictionary_page_offset ?? 0)])),
    })
    expect([meta.row_groups, meta.cells_rows, meta.membership.max_rows]).toEqual([idx.rows.length, rows.length, 4])
  })

  it('finds exactly the members (`gen.py`\'s definition), with their range rows', async () => {
    const cat = new StaticCatalog(files())
    const found = Object.fromEntries(await Promise.all(terms().map(async t => [t, (await cat.lookup(t)).member?.rows ?? null] as const)))
    expect(found).toEqual(members)
    expect(Object.entries(members).filter(([, v]) => v !== null).map(([t]) => t)).toEqual(['foo', 'f', 'fo', 'o', '-', 'parquet', '.parquet', 'quet'])
  })

  it.each(['foo', 'f', 'fo', 'o', '-', 'parquet', '.parquet', 'quet'])('answers member %s from its cells exactly like the first-hit oracle', async t => {
    const { member } = await new StaticCatalog(files()).lookup(t)
    expect(num(catalogAnswer(member!, DATES))).toEqual(expected[t])
  })

  it('reads meta and the index once, then one range per lookup; a key between groups reads nothing', async () => {
    const log: string[] = []
    const cat = new StaticCatalog(files(log))
    const meta = JSON.parse(new TextDecoder().decode(bytes('catalog/meta.json'))) as CatalogMeta
    const idx = await catalogIndex(bytes('catalog/index.parquet'), meta)
    const span = (q: string) => { const [a, b] = catalogGroups(idx, q); return `catalog/cells.parquet@${idx.offset[a]}+${idx.offset[b - 1] + idx.length[b - 1] - idx.offset[a]}` }
    const a = await cat.lookup('foo'), b = await cat.lookup('quet'), c = await cat.lookup('zq')
    expect(catalogGroups(idx, 'zq')[0] < catalogGroups(idx, 'zq')[1]).toBe(true)
    expect(log).toEqual(['catalog/meta.json', 'catalog/index.parquet@0+' + bytes('catalog/index.parquet').byteLength, span('foo'), span('quet'), span('zq')])
    expect([a.io.index, b.io.index, c.io.index, c.member]).toEqual(['file', 'isolate', 'isolate', null])
    expect([a.member!.cells.length, a.io.groups]).toEqual([5, catalogGroups(idx, 'foo')[1] - catalogGroups(idx, 'foo')[0]])
  })
})

describe('static dispatch', () => {
  it('covers every term the fixture was generated for', () => expect(terms()).toEqual(TERMS))

  it.each(TERMS)('answers %s exactly like the first-hit oracle on every date', async t => {
      const { answers } = await answerKey(fixtureStore(), t, DATES)
      expect(num(answers)).toEqual(expected[t])
    })

  it('sends short literals and members to the catalog, everything else to the suffix postings', async () => {
    const plans = Object.fromEntries(await Promise.all(terms().map(async t => [t, (await answerKey(fixtureStore(), t, DATES)).plan] as const)))
    expect(plans).toEqual(Object.fromEntries(terms().map(t => [t, [...t].length <= 2 || members[t] !== null ? 'catalog' : 'bounded-name-postings'])))
  })

  it('reads a light literal from the shard alone, and a heavy non-member from the catalog then the shard', async () => {
    const shard = (log: string[]) => log.filter(k => k.startsWith('sx/') && !k.includes('@-')).length
    const cat = (log: string[]) => log.filter(k => k.startsWith('catalog/cells')).length
    const light: string[] = [], heavy: string[] = [], member: string[] = [], short: string[] = []
    // `qux`: 4 range rows ≤ V; `foo-2`: 8 > V but 2 exact, not a member; `foo`: a member; `fo`: short.
    await answerKey(fixtureStore(light), 'qux', DATES)
    await answerKey(fixtureStore(heavy), 'foo-2', DATES)
    await answerKey(fixtureStore(member), 'foo', DATES)
    await answerKey(fixtureStore(short), 'fo', DATES)
    expect([light, heavy, member, short].map(l => [cat(l), shard(l)])).toEqual([[0, 1], [1, 1], [1, 0], [1, 0]])
    expect(short.some(k => k.startsWith('shards.json') || k.startsWith('sx/'))).toBe(false)
  })

  it('serves the registry from scans.json, STORE_BUCKETS and the catalog bound', async () => {
    const body = await staticRegistryBody(ENV, fixtureStore())
    expect(body).toEqual({
      schema: 'static-name-registry-v1', logical_store: 'gcs_fleet', generation: STATIC_GEN, max_rows: 4, bucket_paths: BUCKETS,
      dates: DATES, levels: 1, scope: HOT_SCOPE, capabilities: { bucket_drill: false, child_drill: false, fallback: false },
    })
    expect(parseNameRegistry(body)).toEqual({
      dated: true, logical_store: 'gcs_fleet', bucket_paths: BUCKETS, static: { generation: STATIC_GEN, max_rows: 4 },
      dates: DATES.map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'static-names-v1', source_identity: { kind: 'static-names-v1', generation: STATIC_GEN, max_rows: 4 } })),
    })
  })

  const side = (date: string, plan: 'catalog' | 'bounded-name-postings', pattern: string, totals: Record<string, [number, number]>) => {
    const buckets = BUCKETS.map((path, i) => ({ path, pre: i + 1, post: i + 1, b: totals[path]?.[0] ?? 0, o: totals[path]?.[1] ?? 0 }))
    return {
      schema: 'dated-name-summary-v1', logical_store: 'gcs_fleet', target: 'static_names', date, pattern, path: '', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
      plan, source: plan === 'catalog' ? STATIC_CATALOG_SOURCE : STATIC_SOURCE, source_identity: { kind: 'static-names-v1', generation: STATIC_GEN, max_rows: 4 },
      validation: { description: `exact first-hit totals from the static name index (generation ${STATIC_GEN}: suffix postings and catalog, rebuilt from the scans and checked against brute force offline); no per-request source oracle`, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false },
      capabilities: { bucket_drill: false, child_drill: false, fallback: false },
      root: { b: buckets.reduce((a, x) => a + x.b, 0), o: buckets.reduce((a, x) => a + x.o, 0) }, buckets,
    }
  }

  it('answers a member (lowercased) from the catalog in the dated contract, bound to the registry', async () => {
    const s = fixtureStore()
    const response = await staticSummary(ENV, params({ date: '2026-09-01', name: 'FOO' }), s)
    expect([response.status, response.headers.get('x-query-engine'), response.headers.get('cache-control')]).toEqual([200, 'name-summary-static', 'private, no-store'])
    const body = await response.json()
    expect(body).toEqual(side('2026-09-01', 'catalog', 'foo', { 'bkt-a': [1061, 12], 'bkt-b': [85, 2] }))
    const registry = parseNameRegistry(await staticRegistryBody(ENV, s)), request = { date: '2026-09-01', name: 'foo' }
    expect(nameResultForRegistry(parseName(body, request), registry).after.root).toEqual({ b: 1146, o: 14 })
  })

  it('answers a diff from the suffix postings, both sides on the same plan', async () => {
    const response = await staticSummary(ENV, params({ from: '2026-08-01', date: '2026-10-01', name: 'bkt' }), fixtureStore())
    const body = await response.json() as Record<string, unknown>
    const before = side('2026-08-01', 'bounded-name-postings', 'bkt', expected.bkt['2026-08-01']), after = side('2026-10-01', 'bounded-name-postings', 'bkt', expected.bkt['2026-10-01'])
    expect(body).toEqual({
      schema: 'dated-name-summary-diff-v1', logical_store: 'gcs_fleet', from: '2026-08-01', date: '2026-10-01', pattern: 'bkt', path: '', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
      before, after, delta: { b: after.root.b - before.root.b, o: after.root.o - before.root.o }, capabilities: { bucket_drill: false, child_drill: false, fallback: false },
      buckets: before.buckets.map((a, i) => { const b = after.buckets[i]; return { path: a.path, before: { pre: a.pre, post: a.post, b: a.b, o: a.o }, after: { pre: b.pre, post: b.post, b: b.b, o: b.o }, delta: { b: b.b - a.b, o: b.o - a.o } } }),
    })
    expect([before.root, after.root]).toEqual([{ b: 30_000, o: 300 }, { b: 30_000, o: 300 }])
  })

  it('flags a dir-only (v1) scan\'s totals `dirs_only` (its folder-name matches), and refuses a diff across a dir-only and a full scan', async () => {
    const v1 = (dirs: string[]): Store => ({ ...fixtureStore(), dirOnly: async () => dirs })
    const one = await staticSummary(ENV, params({ date: '2026-08-01', name: 'bkt' }), v1(['2026-08-01']))
    const body = await one.json()
    expect([one.status, body]).toEqual([200, { ...side('2026-08-01', 'bounded-name-postings', 'bkt', expected.bkt['2026-08-01']), dirs_only: true }])
    const parsed = parseName(body, { date: '2026-08-01', name: 'bkt' })
    expect(parsed.execution.after.dirs_only).toEqual(true)
    // A full scan beside it: unflagged.
    const full = await staticSummary(ENV, params({ date: '2026-10-01', name: 'bkt' }), v1(['2026-08-01']))
    expect([full.status, await full.json()]).toEqual([200, side('2026-10-01', 'bounded-name-postings', 'bkt', expected.bkt['2026-10-01'])])
    const split = await staticSummary(ENV, params({ from: '2026-08-01', date: '2026-10-01', name: 'bkt' }), v1(['2026-08-01']))
    expect([split.status, await split.json()]).toEqual([400, {
      error: 'One of these scans lists folders only (files aren’t searchable on it), so a search can’t be compared across the two; compare two scans that both list files. This is not a zero-match result.',
      code: 'scan-dirs-only',
    }])
    // Both sides dir-only: comparable, each side flagged.
    const both = await staticSummary(ENV, params({ from: '2026-08-01', date: '2026-09-01', name: 'bkt' }), v1(['2026-08-01', '2026-09-01']))
    const diff = await both.json() as { before: { dirs_only?: true }; after: { dirs_only?: true } }
    expect([both.status, diff.before.dirs_only, diff.after.dirs_only]).toEqual([200, true, true])
    const result = parseName(diff, { from: '2026-08-01', date: '2026-09-01', name: 'bkt' })
    expect([result.execution.before?.dirs_only, result.execution.after.dirs_only]).toEqual([true, true])
    // The contract: `dirs_only` is `true` or absent.
    expect(() => parseName({ ...(body as object), dirs_only: false }, { date: '2026-08-01', name: 'bkt' })).toThrow()
  })

  it('refuses a scan outside the generation (400) and fails closed (503) when the index is unreadable', async () => {
    const outside = await staticSummary(ENV, params({ date: '2026-09-02', name: 'foo' }), fixtureStore())
    expect([outside.status, await outside.json()]).toEqual([400, { error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }])
    // With an index to ask: a scan the store has but the static index doesn't is that 400; a slug naming no scan is a 404.
    const db = (d: string | null) => ({ prepare: () => ({ bind: () => ({ first: async () => ({ d }), all: async () => ({ results: d ? [{ date: d }] : [] }) }) }) }) as unknown as D1Database
    const unindexed = await staticSummary({ ...ENV, DB: db('2026-09-02') }, params({ date: '2026-09-02', name: 'foo' }), fixtureStore())
    expect([unindexed.status, await unindexed.json()]).toEqual([400, { error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }])
    const miss = await staticSummary({ ...ENV, DB: db(null) }, params({ date: '2026-09-02', name: 'foo' }), fixtureStore())
    expect([miss.status, await miss.json()]).toEqual([404, { error: 'no scan matches date=2026-09-02' }])
    // The store's scans decide what a day names, as on the map (`scanArg.ts` `indexedScan`): the index's
    // latest scan of a day is never answered for a newer store scan of that day.
    const storeDb = (scans: string[]) => ({ prepare: (sql: string) => ({ bind: (arg: string) => ({ first: async () => {
      const hits = scans.filter(d => sql.includes('LIKE') ? d.startsWith(arg.slice(0, -1)) : d === arg).sort()
      return { d: hits.at(-1) ?? null }
    }, all: async () => ({ results: scans.filter(d => d.startsWith(arg.slice(0, -1))).map(date => ({ date })) }) }) }) }) as unknown as D1Database
    const sub = { ...fixtureStore(), scans: async () => [...DATES, '2026-09-20T0430'].sort() }
    const db2 = storeDb([...DATES, '2026-09-20T0430', '2026-09-20T1236'])
    const ask = async (date: string) => { const r = await staticSummary({ ...ENV, DB: db2 }, params({ date, name: 'foo' }), sub); return [r.status, r.status === 200 ? (await r.json() as { date: string }).date : await r.json()] }
    expect(await ask('2026-09-20')).toEqual([400, { error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }])
    expect(await ask('2026-09-20T1236')).toEqual([400, { error: 'This scan is not in the static name index yet. This is not a zero-match result.', code: 'scan-not-indexed' }])
    expect(await ask('2026-09-21')).toEqual([404, { error: 'no scan matches date=2026-09-21' }])
    expect(await ask('2026-09-01')).toEqual([200, '2026-09-01'])
    const broken: Store = { ...fixtureStore(), catalog: new StaticCatalog({ ...files(), json: async () => { throw new Error('gone') } }) }
    const failed = await staticSummary(ENV, params({ date: '2026-09-01', name: 'foo' }), broken)
    expect([failed.status, failed.headers.get('retry-after'), await failed.json()]).toEqual([503, '1', { error: 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.' }])
  })
})
