import { describe, expect, it } from 'vitest'
import { asyncBufferFromFile, parquetMetadataAsync } from 'hyparquet'
import { join } from 'node:path'
import { prefixBounds, readRows, selectRuns } from '../parquet'

const FIXTURE = join(__dirname, 'fixtures', 'fixture.parquet')

// The fixture is 12 rows in 4-row groups, sorted (depth, path):
//   g0: ['.', 'a', 'b', 't1']   g1: ['t2', 'a/c', 'a/f1', 'a/f2']   g2: ['b/h1', 'b/h2', 'a/c/g1', 'a/c/g2']
const paths = (rows: { path: string }[]) => rows.map(r => r.path)

describe('prefixBounds', () => {
  it('is the half-open range holding exactly the descendants', () => {
    expect(prefixBounds('a')).toEqual(['a/', 'a0'])
    expect(prefixBounds('a/c')).toEqual(['a/c/', 'a/c0'])
  })
})

describe('selectRuns', () => {
  it('prunes groups whose every row is deeper than maxDepth', async () => {
    const meta = await parquetMetadataAsync(await asyncBufferFromFile(FIXTURE))
    expect(selectRuns(meta, { maxDepth: 0 })).toEqual([{ rowStart: 0, rowEnd: 4 }])
    expect(selectRuns(meta, { maxDepth: 1 })).toEqual([{ rowStart: 0, rowEnd: 8 }])
    expect(selectRuns(meta, { maxDepth: 3 })).toEqual([{ rowStart: 0, rowEnd: 12 }])
  })

  it('prunes groups whose path range misses the prefix, merging adjacent keeps', async () => {
    const meta = await parquetMetadataAsync(await asyncBufferFromFile(FIXTURE))
    // 'b' ≤ every group's max and < every group's hi ('b0') except… g2 spans a/c/g1..b/h2, so all kept.
    expect(selectRuns(meta, { maxDepth: 3, prefix: 'b' })).toEqual([{ rowStart: 0, rowEnd: 12 }])
    // 't2' sits in g1 only: g0's max 't1' < 't2'; g2's min 'a/c/g1' < 't20' but its max 'b/h2' < 't2'.
    expect(selectRuns(meta, { maxDepth: 3, prefix: 't2' })).toEqual([{ rowStart: 4, rowEnd: 8 }])
  })
})

describe('readRows', () => {
  it('returns the rows at or above maxDepth, in file order', async () => {
    const rows = await readRows(await asyncBufferFromFile(FIXTURE), { maxDepth: 1 })
    expect(paths(rows)).toEqual(['.', 'a', 'b', 't1', 't2'])
    expect(rows[0]).toEqual({ path: '.', size: 3600, mtime: 1_700_000_000, kind: 'dir', parent: '', n_desc: 11, n_children: 4, depth: 0 })
    expect(rows.map(r => typeof r.size)).toEqual(['number', 'number', 'number', 'number', 'number'])
  })

  it('keeps the prefix row and its descendants only', async () => {
    const file = await asyncBufferFromFile(FIXTURE)
    expect(paths(await readRows(file, { maxDepth: 3, prefix: 'a' }))).toEqual(['a', 'a/c', 'a/f1', 'a/f2', 'a/c/g1', 'a/c/g2'])
    expect(paths(await readRows(file, { maxDepth: 2, prefix: 'a' }))).toEqual(['a', 'a/c', 'a/f1', 'a/f2'])
    expect(paths(await readRows(file, { maxDepth: 3, prefix: 'b' }))).toEqual(['b', 'b/h1', 'b/h2'])
    expect(paths(await readRows(file, { maxDepth: 3, prefix: 't2' }))).toEqual(['t2'])
  })
})

// The same rows as a v2 listing (spec `listing-slim.md`): zstd, no `uri`, the
// scan root in the key-value metadata — what the duckdb/stream engines write.
const FIXTURE_V2 = join(__dirname, 'fixtures', 'fixture-v2.parquet')

describe('v2 (zstd, uri-less) blobs', () => {
  it('are zstd-coded', async () => {
    const meta = await parquetMetadataAsync(await asyncBufferFromFile(FIXTURE_V2))
    expect(meta.row_groups.map(rg => rg.columns[0].meta_data?.codec)).toEqual(['ZSTD', 'ZSTD', 'ZSTD'])
  })

  it('read exactly as the v1 blob does', async () => {
    const v1 = await asyncBufferFromFile(FIXTURE)
    const v2 = await asyncBufferFromFile(FIXTURE_V2)
    for (const opts of [{ maxDepth: 0 }, { maxDepth: 1 }, { maxDepth: 3 }, { maxDepth: 3, prefix: 'a' }, { maxDepth: 3, prefix: 't2' }]) {
      expect(await readRows(v2, opts)).toEqual(await readRows(v1, opts))
    }
  })
})
