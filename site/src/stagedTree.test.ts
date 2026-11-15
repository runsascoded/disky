import { describe, expect, it } from 'vitest'
import { stagedTree } from './stagedTree'

describe('stagedTree: staged prefixes as one treemap tree', () => {
  it('a trie on bucket + segments; interiors re-aggregate (newest write and last read the max); missing or empty stats drop out', () => {
    const stats = {
      'gs://b1/ckpt/run-a/': { bytes: 300, objects: 3, mtime: 100 * 86400, last_read: 7, owners: [['ann', 300]] as [string, number][] },
      'gs://b1/ckpt/run-b/': { bytes: 100, objects: 1, mtime: 200 * 86400, owners: [['bob', 60], ['ann', 40]] as [string, number][] },
      'gs://b2/tmp/': { bytes: 50, objects: 5, class_bytes: { 3: 20 } },
      'gs://b2/empty/': { bytes: 0, objects: 0 },
    }
    const t = stagedTree(['gs://b2/tmp/', 'gs://b1/ckpt/run-a/', 'gs://b1/ckpt/run-b/', 'gs://b2/empty/', 'gs://b3/gone/'], stats, 'staged')
    expect(t).toEqual({
      name: 'staged', kind: 'dir', bytes: 450, objects: 9, mtime: 200 * 86400, last_read: 7, owners: [['ann', 340], ['bob', 60]], class_bytes: { 3: 20 },
      children: [
        {
          name: 'b1', kind: 'dir', bytes: 400, objects: 4, mtime: 200 * 86400, last_read: 7, owners: [['ann', 340], ['bob', 60]],
          children: [{
            name: 'ckpt', kind: 'dir', bytes: 400, objects: 4, mtime: 200 * 86400, last_read: 7, owners: [['ann', 340], ['bob', 60]],
            children: [
              { name: 'run-a', kind: 'dir', bytes: 300, objects: 3, mtime: 100 * 86400, last_read: 7, owners: [['ann', 300]] },
              { name: 'run-b', kind: 'dir', bytes: 100, objects: 1, mtime: 200 * 86400, owners: [['bob', 60], ['ann', 40]] },
            ],
          }],
        },
        { name: 'b2', kind: 'dir', bytes: 50, objects: 5, class_bytes: { 3: 20 }, children: [{ name: 'tmp', kind: 'dir', bytes: 50, objects: 5, class_bytes: { 3: 20 } }] },
      ],
    })
  })
})
