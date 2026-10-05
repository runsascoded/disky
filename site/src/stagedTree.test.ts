import { describe, expect, it } from 'vitest'
import { stagedTree } from './stagedTree'

describe('stagedTree: staged prefixes as one treemap tree', () => {
  it('a trie on bucket + segments; interiors re-aggregate; missing or empty stats drop out', () => {
    const stats = {
      'gs://b1/ckpt/run-a/': { b: 300, o: 3, d: 100, a: 7, us: [['ann', 300]] as [string, number][] },
      'gs://b1/ckpt/run-b/': { b: 100, o: 1, d: 200, us: [['bob', 60], ['ann', 40]] as [string, number][] },
      'gs://b2/tmp/': { b: 50, o: 5, cb: { 3: 20 } },
      'gs://b2/empty/': { b: 0, o: 0 },
    }
    const t = stagedTree(['gs://b2/tmp/', 'gs://b1/ckpt/run-a/', 'gs://b1/ckpt/run-b/', 'gs://b2/empty/', 'gs://b3/gone/'], stats, 'staged')
    expect(t).toEqual({
      n: 'staged', k: 'dir', b: 450, o: 9, d: 125, a: 7, us: [['ann', 340], ['bob', 60]], cb: { 3: 20 },
      c: [
        {
          n: 'b1', k: 'dir', b: 400, o: 4, d: 125, a: 7, us: [['ann', 340], ['bob', 60]],
          c: [{
            n: 'ckpt', k: 'dir', b: 400, o: 4, d: 125, a: 7, us: [['ann', 340], ['bob', 60]],
            c: [
              { n: 'run-a', k: 'dir', b: 300, o: 3, d: 100, a: 7, us: [['ann', 300]] },
              { n: 'run-b', k: 'dir', b: 100, o: 1, d: 200, us: [['bob', 60], ['ann', 40]] },
            ],
          }],
        },
        { n: 'b2', k: 'dir', b: 50, o: 5, cb: { 3: 20 }, c: [{ n: 'tmp', k: 'dir', b: 50, o: 5, cb: { 3: 20 } }] },
      ],
    })
  })
})
