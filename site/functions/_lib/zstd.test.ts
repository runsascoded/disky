import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { decompress } from 'fzstd'
import { asyncBufferFromFile, parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import { compressors, WASM_FRAME_MAX, WASM_INITIAL_BYTES, zstdUse } from './zstd'

// A real overlay path-index (zstd since spec `listing-slim.md`); `fixtures/gen.py`.
// `import.meta.url` is untyped under the Workers types this dir compiles with; vitest runs it in Node.
const FIXTURE = new URL('./fixtures/path-index-zstd.parquet', (import.meta as unknown as { url: string }).url).pathname

describe('zstd index parquet', () => {
  it('is zstd-coded', async () => {
    const meta = await parquetMetadataAsync(await asyncBufferFromFile(FIXTURE))
    expect(meta.row_groups.map(rg => rg.columns.map(c => c.meta_data?.codec))).toEqual([Array(11).fill('ZSTD')])
  })

  it('decodes with `compressors`', async () => {
    const file = await asyncBufferFromFile(FIXTURE)
    const rows = await parquetReadObjects({ file, columns: ['path', 'depth', 'b', 'o', 'wb'], compressors })
    expect(rows).toEqual([
      { path: 'bk', depth: 1n, b: 700n, o: 3n, wb: 700n },
      { path: 'bk/a', depth: 2n, b: 300n, o: 2n, wb: 300n },
      { path: 'bk/b', depth: 2n, b: 400n, o: 1n, wb: 400n },
    ])
  })

  it('does not decode without them (hyparquet is Snappy-only)', async () => {
    const file = await asyncBufferFromFile(FIXTURE)
    await expect(parquetReadObjects({ file, columns: ['path'] })).rejects.toThrow('parquet unsupported compression codec: ZSTD')
  })
})

// A frame by hand: single-segment, content size in the header, then blocks as [type, size, payload]. Raw and RLE
// blocks are all these tests need of the format; what a real page looks like is the fixture's frames.
const LE = (n: number, bytes: number) => Array.from({ length: bytes }, (_, i) => Math.floor(n / 256 ** i) % 256)
function frame(content: number, blocks: [number, number, number[]][]): Uint8Array {
  const body = blocks.flatMap(([type, size, payload], i) => [...LE((size << 3) | (type << 1) | (i === blocks.length - 1 ? 1 : 0), 3), ...payload])
  return Uint8Array.from([0x28, 0xb5, 0x2f, 0xfd, 0xe0, ...LE(content, 8), ...body])
}
/** `n` bytes of `byte` in RLE blocks (at most 128 KiB each). */
function rle(n: number, byte = 7): Uint8Array {
  const sizes = Array.from({ length: Math.ceil(n / 131072) }, (_, i) => Math.min(131072, n - i * 131072))
  return frame(n, sizes.map(s => [1, s, [byte]]))
}
const zstd = compressors.ZSTD!
const FOUR_NINES = frame(4, [[1, 4, [9]]])
/** What `zstd` writes for empty input: the frame an empty page holds. */
const EMPTY_FRAME = Uint8Array.from([0x28, 0xb5, 0x2f, 0xfd, 0x20, 0x00, 0x01, 0x00, 0x00])

const MODULE_PATH = new URL('./vendor/zstd.wasm', (import.meta as unknown as { url: string }).url).pathname
const moduleBytes = async () => {
  const file = await asyncBufferFromFile(MODULE_PATH)
  return file.slice(0, file.byteLength)
}
// Abstract under the Workers types (a Worker can't compile at runtime); this runs in Node.
const compile = (bytes: BufferSource) => new (WebAssembly.Module as unknown as new (b: BufferSource) => WebAssembly.Module)(bytes)
const zstdModule = async () => compile(await moduleBytes())

/** Every frame hyparquet hands the decoder for the fixture, with fzstd's answer. */
async function fixtureFrames(): Promise<[Uint8Array, number, Uint8Array][]> {
  const got: [Uint8Array, number, Uint8Array][] = []
  const file = await asyncBufferFromFile(FIXTURE)
  await parquetReadObjects({ file, compressors: { ZSTD: (i, n) => {
    const out = decompress(i, new Uint8Array(n))
    got.push([i.slice(), n, out])
    return out
  } } })
  return got
}
const heap = (i: WebAssembly.Instance) => (i.exports as { memory: WebAssembly.Memory }).memory.buffer.byteLength
/** The instances `zstdUse` makes, so a test can read the module's memory. */
function watchInstances(): WebAssembly.Instance[] {
  const made: WebAssembly.Instance[] = []
  const Real = WebAssembly.Instance
  vi.stubGlobal('WebAssembly', Object.assign(Object.create(WebAssembly), { Instance: class extends (Real as unknown as new (m: WebAssembly.Module, i?: WebAssembly.Imports) => WebAssembly.Instance) { constructor(m: WebAssembly.Module, i?: WebAssembly.Imports) { super(m, i); made.push(this) } } }))
  return made
}
const fakeModule = (exports: unknown) => {
  vi.stubGlobal('WebAssembly', Object.assign(Object.create(WebAssembly), { Instance: class { exports = exports } }))
}

describe('the wasm decoder', () => {
  let logs: string[]
  beforeEach(() => {
    logs = []
    vi.spyOn(console, 'log').mockImplementation((m: string) => { logs.push(m) })
  })
  afterEach(() => {
    zstdUse(null)
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('is the pinned module', async () => {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await moduleBytes()))
    expect(Array.from(digest, b => b.toString(16).padStart(2, '0')).join('')).toBe('ae3e30fb277e4830b49acee81b27b32467e70307c0ebdde7bc1a5c230cdbb2a7')
  })

  it("answers the fixture's frames with fzstd's bytes", async () => {
    const frames = await fixtureFrames()
    expect(frames.length).toBe(11)
    zstdUse(await zstdModule())
    for (const [input, n, want] of frames) expect(zstd(input, n)).toEqual(want)
    expect(logs).toEqual([])
  })

  it('reads the fixture to the same rows', async () => {
    zstdUse(await zstdModule())
    const rows = await parquetReadObjects({ file: await asyncBufferFromFile(FIXTURE), columns: ['path', 'depth', 'b', 'o', 'wb'], compressors })
    expect(rows).toEqual([
      { path: 'bk', depth: 1n, b: 700n, o: 3n, wb: 700n },
      { path: 'bk/a', depth: 2n, b: 300n, o: 2n, wb: 300n },
      { path: 'bk/b', depth: 2n, b: 400n, o: 1n, wb: 400n },
    ])
    expect(logs).toEqual([])
  })

  it('decodes an empty page, raw and RLE blocks, and a page after an empty one, as fzstd does', async () => {
    const cases: [Uint8Array, number, number[]][] = [
      [EMPTY_FRAME, 0, []],
      [frame(5, [[0, 5, [1, 2, 3, 4, 5]]]), 5, [1, 2, 3, 4, 5]],
      [FOUR_NINES, 4, [9, 9, 9, 9]],
      [EMPTY_FRAME, 0, []],
    ]
    for (const use of [null, await zstdModule()]) {
      zstdUse(use)
      expect(cases.map(([input, n]) => [...zstd(input, n)])).toEqual(cases.map(c => c[2]))
    }
    expect(logs).toEqual([])
  })

  it('never grows its heap: the largest frame it takes, then many small ones', async () => {
    const made = watchInstances()
    zstdUse(await zstdModule())
    const n = WASM_FRAME_MAX - 16 * 1024
    const big = rle(n)
    expect(big.length + n).toBeLessThanOrEqual(WASM_FRAME_MAX)
    const out = zstd(big, n)
    expect([out.length, out[0], out[n - 1]]).toEqual([n, 7, 7])
    for (let i = 1; i <= 2000; i++) zstd(rle(i * 37), i * 37)
    expect(made.map(heap)).toEqual([WASM_INITIAL_BYTES])
    expect(logs).toEqual([])
  })

  it('leaves a frame past the cap to fzstd: same bytes, no module made', async () => {
    const made = watchInstances()
    zstdUse(await zstdModule())
    const n = WASM_INITIAL_BYTES * 2
    const out = zstd(rle(n, 3), n)
    expect([out.length, out[0], out[n - 1]]).toEqual([n, 3, 3])
    expect(made).toEqual([])
  })

  it('leaves a frame it refuses to fzstd, whose error stands, and stays on', async () => {
    const made = watchInstances()
    zstdUse(await zstdModule())
    const bad = Uint8Array.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    expect(() => decompress(bad, new Uint8Array(4))).toThrow('invalid zstd data')
    expect(() => zstd(bad, 4)).toThrow('invalid zstd data')
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect(made.length).toBe(1)
    expect(logs).toEqual([])
  })

  it('falls back to fzstd, once and logged, when the module will not instantiate', () => {
    // A module asking for an import `zstdUse` doesn't supply: instantiating throws a LinkError.
    zstdUse(compile(Uint8Array.from([0, 97, 115, 109, 1, 0, 0, 0, 1, 4, 1, 96, 0, 0, 2, 7, 1, 1, 120, 1, 121, 0, 0])))
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect(logs).toEqual(['zstd wasm: instantiating failed, fzstd from here on: WebAssembly.Instance(): Import #0 "x": module is not an object or function'])
  })

  it('falls back to fzstd, once and logged, when decoding traps', async () => {
    let decodes = 0
    fakeModule({ memory: new WebAssembly.Memory({ initial: 1 }), malloc: () => 16, free: () => {}, ZSTD_decompress: () => { decodes++; throw new WebAssembly.RuntimeError('unreachable') } })
    zstdUse(await zstdModule())
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect(decodes).toBe(1)
    expect(logs).toEqual(['zstd wasm: decoding trapped, fzstd from here on: unreachable'])
  })

  it('leaves a frame to fzstd when the module has no memory for it, and frees what it took', async () => {
    const freed: number[] = []
    fakeModule({ memory: new WebAssembly.Memory({ initial: 1 }), malloc: (n: number) => n === 4 ? 0 : 32, free: (p: number) => { freed.push(p) }, ZSTD_decompress: () => { throw new Error('must not be reached') } })
    zstdUse(await zstdModule())
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect(freed).toEqual([32, 0])
    expect(logs).toEqual([])
  })

  it('with the flag off, never instantiates', () => {
    const made = watchInstances()
    zstdUse(null)
    expect([...zstd(FOUR_NINES, 4)]).toEqual([9, 9, 9, 9])
    expect(made).toEqual([])
  })
})
