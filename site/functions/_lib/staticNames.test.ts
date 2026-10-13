import { parquetMetadata } from 'hyparquet'
import { beforeAll, describe, expect, it } from 'vitest'
import { type Blobs, cmp, type GroupIndex, groupIndex, type IndexCache, selectGroups, shardFor, StaticNames } from './staticNames.js'
import { fixture } from './testStore.js'

/** `gen.py`'s `TERMS`. */
const TERMS = ['foo', 'foo.', 'foo-2', 'oof', 'fooz', 'qux', 'mmm', 'andú', 'abcdefghijklmnopqrstuvwxyz-0001', 'bkt', 'zzz',
  'f', 'fo', 'o', '-', 'zq', 'bkt-a', 'abcdefghijklmnopqrstuvwxyz-000', 'abcdefghijklmnopqrstuvwxyz-0002.parquet', 'parquet', '.parquet', 'quet']
/** The suffix shards hold suffixes of three or more characters only. */
const LONG = TERMS.filter(t => [...t].length >= 3)
const KEYS = ['shards.json', 'expected.json', 'sx/s0000.parquet', 'sx/s0001.parquet']
const held = new Map<string, ArrayBuffer>()
beforeAll(async () => {
  // Node-only, imported dynamically: the Workers tsconfig has no Node types (`testStore.ts`).
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of KEYS) { const b = fs.readFileSync(fixture(`static-names/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  expected = JSON.parse(new TextDecoder().decode(held.get('expected.json')))
})
const bytes = (key: string) => held.get(key)!
/** The fixture as `Blobs`, recording each read (`key@offset+length`). */
function files(log: string[] = []): Blobs {
  return {
    async range(key, offset, length) { const b = bytes(key); const end = length == null ? b.byteLength : offset + length; log.push(`${key}@${offset}+${end - offset}`); return b.slice(offset, end) },
    async suffix(key, n) { const b = bytes(key), size = b.byteLength; log.push(`${key}@-${n}`); return { buf: b.slice(Math.max(0, size - n)), size } },
    async json(key) { log.push(key); return JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
/** The brute-force first-hit oracle's answers (`gen.py`), `[bytes, objects]` per bucket. */
let expected: Record<string, Record<string, Record<string, [number, number]>>>
const num = (answers: Record<string, Record<string, [bigint, bigint]>>) =>
  Object.fromEntries(Object.entries(answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))

describe('static names reader', () => {
  it('orders strings by code point, as the shards are sorted', () => {
    expect(['\u{1F600}', '�', 'z', 'a'].sort(cmp)).toEqual(['a', 'z', '�', '\u{1F600}'])
    expect(cmp('foo', 'foo\u{10FFFF}') < 0 && cmp('foo\u{10FFFF}', 'fop') < 0).toBe(true)
  })

  it('maps a key to the shard holding its first three characters', async () => {
    const shards = await new StaticNames(files()).plan()
    expect(['foo', 'mmm', 'm', 'zzz', 'abc', '  a', '\t'].map(k => shardFor(shards, k)?.i ?? null)).toEqual([0, 1, 1, 1, 0, 0, null])
  })

  it('cuts a group index from the footer equal to hyparquet\'s full parse of it', () => {
    for (const f of ['sx/s0000.parquet', 'sx/s0001.parquet']) {
      const b = bytes(f), size = b.byteLength
      const flen = new DataView(b, size - 8).getUint32(0, true)
      const md = parquetMetadata(b)
      const meta = md.row_groups.map(rg => rg.columns.map(c => c.meta_data!))
      expect(groupIndex(new Uint8Array(b, size - 8 - flen, flen), size)).toEqual({
        size,
        sMin: meta.map(cs => cs[0].statistics!.min_value),
        sMax: meta.map(cs => cs[0].statistics!.max_value),
        rows: md.row_groups.map(rg => Number(rg.num_rows)),
        chunks: meta.flatMap(cs => cs.flatMap(c => [Number(c.data_page_offset), Number(c.total_compressed_size), Number(c.dictionary_page_offset ?? 0)])),
      })
    }
    const b = bytes('sx/s0000.parquet'), flen = new DataView(b, b.byteLength - 8).getUint32(0, true)
    const idx = groupIndex(new Uint8Array(b, b.byteLength - 8 - flen, flen), b.byteLength)
    expect(idx.rows).toEqual([...Array(24).fill(4), 3])
    expect([selectGroups(idx, 'foo'), selectGroups(idx, 'fooz'), selectGroups(idx, 'abcdefghijklmnopqrstuvwxyz-0001')]).toEqual([[17, 21], [20, 21], [10, 11]])
  })

  it('covers every term the fixture was generated for', () => expect(Object.keys(expected)).toEqual(TERMS))

  it.each(LONG)('answers %s exactly like the first-hit oracle on every date', async term => {
    const r = new StaticNames(files())
    const { answer } = await r.answer(term, Object.keys(expected[term]))
    expect(num(answer!.answers)).toEqual(expected[term])
  })

  it('reads the plan once, a footer once per shard, then one range per query', async () => {
    const log: string[] = []
    const r = new StaticNames(files(log))
    const dates = ['2026-09-01']
    const a = await r.answer('foo', dates), b = await r.answer('foo.', dates), c = await r.answer('qux', dates)
    expect(log).toEqual([
      'shards.json',
      'sx/s0000.parquet@-67136',
      `sx/s0000.parquet@${log[2].split('@')[1]}`,
      `sx/s0000.parquet@${log[3].split('@')[1]}`,
      'sx/s0001.parquet@-67136',
      `sx/s0001.parquet@${log[5].split('@')[1]}`,
    ])
    expect([a.io.index, b.io.index, c.io.index]).toEqual(['footer', 'isolate', 'footer'])
    expect([a.io.groups, a.io.rows_read, a.io.rows_matching]).toEqual([4, 16, 11])
    expect([b.io.groups, b.io.rows_read, b.io.rows_matching]).toEqual([1, 4, 3])
  })

  it('refuses a range above the row budget without fetching data', async () => {
    const log: string[] = []
    const { io, answer } = await new StaticNames(files(log)).answer('foo', ['2026-09-01'], 15)
    expect([answer, io.groups, io.rows_read, io.bytes]).toEqual([null, 4, 16, 0])
    expect(log).toEqual(['shards.json', 'sx/s0000.parquet@-67136'])
  })

  it('serves the group index from the shared cache in a fresh isolate', async () => {
    const store = new Map<string, GroupIndex>()
    const cache: IndexCache = { get: async f => store.has(f) ? JSON.parse(JSON.stringify(store.get(f))) : null, put: async (f, idx) => { store.set(f, idx) } }
    await new StaticNames(files(), cache).answer('foo', ['2026-09-01'])
    const log: string[] = []
    const { io, answer } = await new StaticNames(files(log), cache).answer('foo', ['2026-09-01'])
    expect(io.index).toBe('cache')
    expect(log.length).toBe(2)
    expect(num(answer!.answers)).toEqual({ '2026-09-01': expected.foo['2026-09-01'] })
  })
})
