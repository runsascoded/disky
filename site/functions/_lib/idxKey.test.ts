import { describe, expect, it } from 'vitest'
import { idxKey } from './ownerBands'

describe('idxKey', () => {
  it('a bucket prefix keys under its bucket; a file store path under the root, no leading `/`', () => {
    expect([
      idxKey('gs://marin-b/x/y/'),
      idxKey('gs://marin-b/'),
      idxKey('file:///Users/ryan/Downloads/Photos-1-001 (2).zip'),
      idxKey('file:///Users/ryan/Downloads/'),
    ]).toEqual([
      { path: 'marin-b/x/y', depth: 3 },
      { path: 'marin-b', depth: 1 },
      { path: 'Users/ryan/Downloads/Photos-1-001 (2).zip', depth: 4 },
      { path: 'Users/ryan/Downloads', depth: 3 },
    ])
  })
})
