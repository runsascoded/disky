import { parquetMetadata, parquetReadObjects } from 'hyparquet'
import { beforeAll, describe, expect, it } from 'vitest'
import { type Blobs, cmp, FirstHits, type GroupIndex, groupIndex, type IndexCache, type Io, selectGroups, shardFor, shardsFor, StaticNames, type SxColumns } from './staticNames.js'
import { fixture } from './testStore.js'
import { compressors } from './zstd.js'

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

describe('a key shorter than the shards\' three-character prefixes (`static-names-split`)', () => {
  /** `static-names-split/gen.py`'s shard files: the same suffix rows, cut inside the prefixes of `f`, `o` and `oo`. */
  const SPLIT = ['shards.json', ...Array.from({ length: 7 }, (_, i) => `sx/s${String(i).padStart(4, '0')}.parquet`)]
  const split = new Map<string, ArrayBuffer>()
  const row = (s: string, path: string, usr: string, vf: unknown) => `${s}|${path}|${usr}|${vf}`
  /** Every suffix row of every shard file, read whole with hyparquet (not through the reader), as `s|path|usr|vf`. */
  let all: string[]
  beforeAll(async () => {
    const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
    for (const k of SPLIT) { const b = fs.readFileSync(fixture(`static-names-split/${k}`)); split.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
    all = []
    for (const k of SPLIT.slice(1)) {
      const rows = await parquetReadObjects({ file: split.get(k)!, columns: ['s', 'path', 'usr', 'vf'], compressors }) as { s: string; path: string; usr: string; vf: unknown }[]
      all.push(...rows.map(r => row(r.s, r.path, r.usr, r.vf instanceof Date ? BigInt(r.vf.getTime()) : r.vf)))
    }
  })
  function splitFiles(log: string[] = []): Blobs {
    return {
      async range(key, offset, length) { const b = split.get(key)!; const end = length == null ? b.byteLength : offset + length; log.push(`${key}@${offset}+${end - offset}`); return b.slice(offset, end) },
      async suffix(key, n) { const b = split.get(key)!, size = b.byteLength; log.push(`${key}@-${n}`); return { buf: b.slice(Math.max(0, size - n)), size } },
      async json(key) { log.push(key); return JSON.parse(new TextDecoder().decode(split.get(key)!)) },
    }
  }
  /** Every row the reader decodes whose `s` starts with the key. */
  class Collect extends FirstHits {
    readonly got: string[] = []
    add(cols: SxColumns): void {
      for (let i = 0; i < cols.s.length; i++) if (cols.s[i].startsWith(this.key)) this.got.push(row(cols.s[i], cols.path[i], cols.usr[i], cols.vf[i]))
    }
  }
  const collect = (k: string) => new Collect(k)
  const noIo = (): Io => ({ shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} })

  it('maps a short key to every shard meeting its prefixes, in order; a long or exact one to its prefix\'s shard', async () => {
    const shards = await new StaticNames(splitFiles()).plan()
    const keys = ['f', 'fo', 'foo', 'o', 'oo', 'oof', 'ox', 'p', 'z']
    expect(keys.map(k => [k, shardsFor(shards, k).map(s => s.i), shardsFor(shards, k, true).map(s => s.i)])).toEqual([
      ['f', [1, 2], [1]], ['fo', [2], [2]], ['foo', [2], [2]], ['o', [3, 4, 5], [3]], ['oo', [4, 5], [4]], ['oof', [5], [5]],
      ['ox', [5], [5]], ['p', [6], [6]], ['z', [6], [6]],
    ])
  })

  it.each(['f', 'fo', 'o', 'oo', 'oof', 'fgh', 'p', 'zq'])('%s reads every shard its rows lie in: exactly its rows', async key => {
    const { fold } = await new StaticNames(splitFiles()).read(key, Infinity, { fold: collect })
    expect((fold as Collect).got.sort()).toEqual(all.filter(r => r.startsWith(key)).sort())
  })

  it('bounds a split key by its shards\' selected groups: refused one row under, exact at the bound', async () => {
    const r = new StaticNames(splitFiles())
    const sel = await r.select('oo', noIo())
    expect([sel!.parts.map(p => p.shard.i), sel!.wide]).toEqual([[4, 5], undefined])
    const refused = await r.read('oo', sel!.rows - 1, { fold: collect })
    expect([refused.fold, refused.io.rows_read, refused.io.bytes]).toEqual([null, sel!.rows, 0])
    const { fold } = await r.read('oo', sel!.rows, { fold: collect })
    expect((fold as Collect).got.sort()).toEqual(all.filter(x => x.startsWith('oo')).sort())
  })

  it('past `maxShards` bounds a key by its shards\' planned rows: refused over them, read whole under them', async () => {
    const r = new StaticNames(splitFiles())
    r.maxShards = 2
    expect(await r.select('o', noIo())).toEqual({ parts: [], rows: 33 + 5 + 4, wide: true })
    expect(await r.extent('o', noIo())).toEqual({ parts: [], rows: 42 })
    const refused = await r.read('o', 41, { fold: collect })
    expect([refused.fold, refused.io.rows_read, refused.io.bytes]).toEqual([null, 42, 0])
    const { fold } = await r.read('o', 42, { fold: collect })
    expect((fold as Collect).got.sort()).toEqual(all.filter(x => x.startsWith('o')).sort())
  })
})
