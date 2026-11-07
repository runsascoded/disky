import { describe, expect, it } from 'vitest'
import { blockOf, blockSpan, viewOf, type BlockIndex } from './extras.js'

const idx: BlockIndex = { v: 1, n: 0, size: 0, keys: ['a/1', 'b/2', 'd/1'], offsets: [0, 100, 200] }
const sized: BlockIndex = { v: 1, n: 3, size: 260, keys: ['a/1', 'b/2', 'd/1'], offsets: [0, 100, 200] }
const empty: BlockIndex = { v: 1, n: 0, size: 0, keys: [], offsets: [] }

describe('blockOf', () => {
  it('finds the block whose first key is ≤ the key', () => {
    expect(blockOf(idx, 'a/1')).toBe(0)
    expect(blockOf(idx, 'a/9')).toBe(0)
    expect(blockOf(idx, 'b/2')).toBe(1)
    expect(blockOf(idx, 'c')).toBe(1)
    expect(blockOf(idx, 'd/1')).toBe(2)
    expect(blockOf(idx, 'z')).toBe(2)
  })
  it('is −1 before the first key', () => {
    expect(blockOf(idx, '0')).toBe(-1)
  })
})

describe('blockSpan', () => {
  it('spans blocks from..to, ending at the next offset or the file size', () => {
    expect(blockSpan(sized, 0, 0)).toEqual([0, 100])
    expect(blockSpan(sized, 1, 2)).toEqual([100, 260])
    expect(blockSpan(sized, 0, 2)).toEqual([0, 260])
    expect(blockSpan(sized, 2, 5)).toEqual([200, 260]) // `to` past the last block clamps to size
  })
  it('is null when there is nothing to read', () => {
    // The regression: an empty sidecar (0 blocks) must not synthesize a range.
    expect(blockSpan(empty, -1, 0)).toBeNull()
    expect(blockSpan(empty, 0, 0)).toBeNull()
    expect(blockSpan(sized, -1, -1)).toBeNull() // key sorts before the first block
    expect(blockSpan(sized, 5, 5)).toBeNull() // `from` past the last block
  })
})

describe('viewOf', () => {
  const v = viewOf(
    ['b/grug\tcalvin\twandb-run\tent/proj/abc', 'b/data\tryan\tmanual\t', 'b/grug/run9\tkevin\t\t'],
  )
  it('resolves provenance from the deepest attributing ancestor with that user', () => {
    expect(v.provenance('b/grug/run1/checkpoints', 'calvin')).toEqual(['wandb-run', 'ent/proj/abc', 'b/grug'])
    expect(v.provenance('b/data/x', 'ryan')).toEqual(['manual', null, 'b/data'])
    expect(v.provenance('b/grug/run9/steps', 'kevin')).toEqual(['unknown', null, 'b/grug/run9'])
  })
  it('is null for another user, or off the map', () => {
    expect(v.provenance('b/grug/run1', 'ryan')).toBeNull()
    expect(v.provenance('c/x', 'calvin')).toBeNull()
  })
})
