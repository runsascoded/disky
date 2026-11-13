import { describe, expect, it } from 'vitest'
import { buildTree, type DiffData } from './diffModel'
import { deltaColor, diffCellStyle, UNCHANGED_GREY } from './diffStyle'

const data: DiffData = { prev: 'old', curr: 'new', bytes_before: 0, bytes_after: 100, objects_before: 0, objects_after: 500, expansions: 0, truncated: false, threshold: 0, lookups: 0, lookups_capped: false, rows: [{ path: '(other)', depth: 1, kind: 'dir', status: 'added', bytes_before: 0, bytes_after: 100, objects_before: 0, objects_after: 500 }] }
describe('diff remainder grids', () => {
  it('uses the larger observed object count as a density hint in both area modes', () => {
    const node = buildTree(data, 'delta', false).cells[0]
    expect([diffCellStyle(node, 'delta').dustCount, diffCellStyle({ ...node, n_old: 900 }, 'max').dustCount]).toEqual([500, 900])
    expect([diffCellStyle({ ...node, label: 'real' }, 'delta').dustCount, diffCellStyle({ ...node, status: 'filler' }, 'max').dustCount]).toEqual([undefined, undefined])
  })
  it('describes the max-mode change band as renderer-independent fill data', () => {
    const node = { ...buildTree(data, 'max', false).cells[0], size_old: 80, size_new: 100, weight: 100, delta: 20 }
    expect(diffCellStyle(node, 'max').bands).toEqual([
      { color: UNCHANGED_GREY, frac: .8 }, { color: deltaColor(1), frac: .2 },
    ])
  })
})
