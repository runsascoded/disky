import { describe, expect, it } from 'vitest'
import { prefixPattern } from './prefixPattern'

describe('prefixPattern', () => {
  it("a dir prefix in one of the store's buckets", () => {
    const re = prefixPattern({ scheme: 'gs://', buckets: ['data-a', 'data.b'] })
    expect(['gs://data-a/', 'gs://data-a/x/y/', 'gs://data.b/x/', 'gs://data-a/x', 'gs://dataxb/x/', 'gs://other/x/', 's3://data-a/x/', 'gs://data-a/a b/'].map(p => re.test(p)))
      .toEqual([true, true, true, false, false, false, false, false])
  })
  it('a store with no buckets admits any bucket', () => {
    const re = prefixPattern({ scheme: 's3://', buckets: [] })
    expect(['s3://anything/x/', 's3:///x/', 'gs://anything/x/'].map(p => re.test(p))).toEqual([true, false, false])
  })
})
