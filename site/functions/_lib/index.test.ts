import { describe, expect, it } from 'vitest'
import { AGE_COLS, blobKey, chunkSpan, groupMatches, planRuns, storeCreds, storePrefixes, storeReady, storeScheme, storeTarget, toRow } from './index'

// The blob handle's in-memory span selection must be the predicate
// `selectSpans` sends D1 (index.ts), NULL semantics included.
const rect = { dLo: 2, dHi: 2, pLo: 'marin-b/x', pHi: 'marin-b/x0' }
const single = { dMin: 2, dMax: 2, pMin: 'marin-b/a', pMax: 'marin-b/m', bMax: 5_000 }
const boundary = { dMin: 1, dMax: 3, pMin: 'marin-a/z', pMax: 'marin-b/c', bMax: 5_000 }

describe('groupMatches', () => {
  it('a single-depth group needs a path overlap; a depth-boundary group only the depth', () => {
    expect(groupMatches(single, [rect])).toBe(false)
    expect(groupMatches({ ...single, pMin: 'marin-b/w', pMax: 'marin-b/z' }, [rect])).toBe(true)
    expect(groupMatches(boundary, [rect])).toBe(true)
    expect(groupMatches({ ...boundary, dMin: 3, dMax: 4 }, [rect])).toBe(false)
  })
  it('any rect may hold the group', () => {
    expect(groupMatches(single, [rect, { dLo: 1, dHi: 3, pLo: '', pHi: '\uffff' }])).toBe(true)
  })
  it('prunes by the floored byte threshold', () => {
    expect(groupMatches(boundary, [rect], 5_000.7)).toBe(true)
    expect(groupMatches(boundary, [rect], 5_001)).toBe(false)
  })
  it('a lens needs the usr range to cover the key; a single-user group also needs the rect', () => {
    const lens = { key: 'kim' }
    expect(groupMatches({ ...single, uMin: null, uMax: null }, [rect], 0, lens)).toBe(false)
    expect(groupMatches({ ...single, uMin: 'alice', uMax: 'zed' }, [rect], 0, lens)).toBe(true)
    expect(groupMatches({ ...single, uMin: 'lee', uMax: 'zed' }, [rect], 0, lens)).toBe(false)
    expect(groupMatches({ ...single, uMin: 'kim', uMax: 'kim' }, [rect], 0, lens)).toBe(false)
    expect(groupMatches({ ...single, uMin: 'kim', uMax: 'kim', pMin: 'marin-b/w', pMax: 'marin-b/z' }, [rect], 0, lens)).toBe(true)
  })
})

describe('blobKey', () => {
  it('sits beside the tier parquet', () => {
    expect(blobKey('listing/2026-09-07/index/20260907T070112Z', 'coarse24-user')).toBe('listing/2026-09-07/index/20260907T070112Z/path-index-coarse24-by-user.groups.json')
    expect(blobKey('listing/2026-07-30', 'path')).toBe('listing/2026-07-30/path-index.groups.json')
  })
})

// The store seam: `STORE_BUCKET` + the GCS endpoint and HMAC pair by default;
// `STORE_*` points every proxy at an S3-compatible store (R2) —
// specs/done/r2-serving-migration.md. The bucket has no default
// (specs/oa-decoupling.md step 3).
describe('store seam', () => {
  const gcs = { GCS_HMAC_KEY_ID: 'gk', GCS_HMAC_SECRET: 'gs', STORE_BUCKET: 'my-data' } as never
  const r2 = { ...(gcs as object), STORE_ENDPOINT: 'https://acct.r2.cloudflarestorage.com', STORE_BUCKET: 'idx', STORE_REGION: 'auto', STORE_ACCESS_KEY_ID: 'rk', STORE_SECRET_ACCESS_KEY: 'rs' } as never
  it('defaults to GCS + the HMAC pair', () => {
    expect(storeTarget(gcs)).toEqual({ endpoint: 'https://storage.googleapis.com', bucket: 'my-data', region: 'us-east1' })
    expect(storeCreds(gcs)).toEqual({ accessKeyId: 'gk', secretAccessKey: 'gs' })
    expect(storeReady(gcs)).toBe(true)
  })
  it('STORE_* overrides target and creds together', () => {
    expect(storeTarget(r2)).toEqual({ endpoint: 'https://acct.r2.cloudflarestorage.com', bucket: 'idx', region: 'auto' })
    expect(storeCreds(r2)).toEqual({ accessKeyId: 'rk', secretAccessKey: 'rs' })
  })
  it('names the store by its endpoint family', () => {
    expect(storeScheme('https://storage.googleapis.com')).toBe('gs')
    expect(storeScheme('https://acct.r2.cloudflarestorage.com')).toBe('r2')
    expect(storeScheme('https://s3.us-east-1.amazonaws.com')).toBe('s3')
  })
  it('is not ready without a bucket: no deployment\'s bucket is a default', () => {
    expect(storeTarget({ GCS_HMAC_KEY_ID: 'gk', GCS_HMAC_SECRET: 'gs' } as never).bucket).toBe('')
    expect(storeReady({ GCS_HMAC_KEY_ID: 'gk', GCS_HMAC_SECRET: 'gs' } as never)).toBe(false)
  })
  it('is not ready without a full credential pair', () => {
    expect(storeReady({} as never)).toBe(false)
    expect(storeReady({ STORE_ACCESS_KEY_ID: 'rk' } as never)).toBe(false)
  })
  it('STORE_PREFIXES replaces each proxy\'s default allow-list; unset keeps it', () => {
    expect(storePrefixes(gcs, ['listing/', 'snapshots/'])).toEqual(['listing/', 'snapshots/'])
    expect(storePrefixes(gcs, ['listing/'])).toEqual(['listing/'])
    const cw = { ...(gcs as object), STORE_PREFIXES: 'listing/, snapshots/,sweep/,cw-sweep/,cw-l2/,' } as never
    expect(storePrefixes(cw, ['listing/'])).toEqual(['listing/', 'snapshots/', 'sweep/', 'cw-sweep/', 'cw-l2/'])
  })
})

describe('toRow: bytes by age', () => {
  const base = { path: 'b/x', depth: 2, usr: null, kind: 'dir', size: 70, n_files: 3, n_children: 2, n_desc: 3, mtime: 0, mtime_mean: null, last_read: null, sum_storage_class_id_2: 0, sum_storage_class_id_3: 0, sum_storage_class_id_4: 0 }
  it('a generation with the age columns decodes them in bucket order', () => {
    const r = toRow({ version: 2 })({ ...base, ...Object.fromEntries(AGE_COLS.map((c, i) => [c, BigInt(i * 10)])) })
    expect(r.ages).toEqual([0, 10, 20, 30, 40, 50, 60])
  })
  it('one without them decodes `ages: null`', () => {
    expect(toRow({ version: 2 })(base).ages).toBe(null)
  })
})

describe('planRuns', () => {
  const sp = [{ start: 300, end: 400 }, { start: 0, end: 100 }, { start: 150, end: 250 }, { start: 1000, end: 1100 }]
  it('merges spans in byte order while the gap is within `gap`', () => {
    expect(planRuns(sp, 50, 1000)).toEqual([{ start: 0, end: 400, items: [sp[1], sp[2], sp[0]] }, { start: 1000, end: 1100, items: [sp[3]] }])
    expect(planRuns(sp, 49, 1000).map(r => [r.start, r.end])).toEqual([[0, 100], [150, 250], [300, 400], [1000, 1100]])
  })
  it('a run stays within `max` bytes; a bigger span is a run of its own', () => {
    expect(planRuns(sp, 50, 300).map(r => [r.start, r.end])).toEqual([[0, 250], [300, 400], [1000, 1100]])
    expect(planRuns([{ start: 0, end: 10 }, { start: 10, end: 500 }], 0, 100).map(r => [r.start, r.end])).toEqual([[0, 10], [10, 500]])
  })
  it('no spans, no runs', () => {
    expect(planRuns([])).toEqual([])
  })
})

describe('chunkSpan', () => {
  const col = (name: string, data: number, size: number, dict?: number) => ({ meta_data: { path_in_schema: [name], data_page_offset: BigInt(data), total_compressed_size: BigInt(size), ...(dict != null ? { dictionary_page_offset: BigInt(dict) } : {}) } })
  const rg = { columns: [col('a', 4, 10), col('b', 20, 30, 14), col('c', 44, 6)] }
  it('from the first chunk (its dictionary page, if any) to the last chunk’s end', () => {
    expect(chunkSpan(rg)).toEqual([4, 50])
    expect(chunkSpan(rg, ['b'])).toEqual([14, 44])
    expect(chunkSpan(rg, ['a', 'c'])).toEqual([4, 50])
  })
  it('no chunk to read is an error', () => {
    expect(() => chunkSpan(rg, ['z'])).toThrow('row group has no column chunks to read')
  })
})
