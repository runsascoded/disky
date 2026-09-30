import { describe, expect, it } from 'vitest'
import { asyncBufferFromFile, parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import { compressors } from './zstd'

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
