/** `.groups.json` footer sidecar (`ui/cfn/groups.ts`): the serverless reader
 *  plans range reads from a precomputed footer instead of parsing the ~5 MB
 *  thrift footer on a cold isolate. The contract is *identity* — the revived
 *  metadata must read exactly what a real footer parse reads — so a stale or
 *  wrong sidecar fails loudly here rather than silently serving wrong rows. */
import { describe, expect, it } from 'vitest'
import { join } from 'node:path'
import { dirBucket } from './fakeR2'
import { r2Buffer, readRows } from '../parquet'
import type { Query } from '../parquet'
import { loadGroups, metaFor, reviveMetadata } from '../groups'

const PREFIX = 'scans/'
const bucket = dirBucket(join(__dirname, 'fixtures'), PREFIX)
const KEY = PREFIX + 'fixture.parquet'

const fileFor = async () => {
  const head = await bucket.head(KEY)
  return r2Buffer(bucket, KEY, head!.size)
}

const QUERIES: Query[] = [
  { maxDepth: 10 },               // whole tree
  { maxDepth: 1 },                // top level only
  { maxDepth: 2, prefix: 'a' },   // a subtree, prefix-pruned
  { maxDepth: 3, prefix: 'a/c' }, // a deeper prefix
]

describe('groups.json footer sidecar', () => {
  it('the fixture ships a v1 sidecar with one entry per row group', async () => {
    const doc = await loadGroups(bucket, KEY)
    expect(doc).not.toBeNull()
    // fixture.parquet is 12 rows in 4-row groups → 3 groups; a main blob has no floor.
    expect([doc!.v, doc!.groups.length, doc!.floor_bytes]).toEqual([1, 3, null])
  })

  it('reads identically to a real thrift-footer parse, for every query', async () => {
    const meta = reviveMetadata((await loadGroups(bucket, KEY))!)
    for (const q of QUERIES) {
      const viaFooter = await readRows(await fileFor(), q)
      const viaGroups = await readRows(await fileFor(), q, meta)
      expect(viaGroups).toEqual(viaFooter)
    }
  })

  it('metaFor is undefined when the sidecar is absent → the caller footer-parses', async () => {
    expect(await metaFor(bucket, PREFIX + 'nope.parquet')).toBeUndefined()
  })
})
