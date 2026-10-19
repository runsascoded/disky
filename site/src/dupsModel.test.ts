import { describe, expect, it } from 'vitest'
import { type DupCopy, type DupGroup, groupIdle, idleDays, pairMatrix, sortGroups, stageBlock, stageItems, suggested } from './dupsModel'

const copy = (path: string, o: Partial<DupCopy> = {}): DupCopy => ({
  path, rel: path.slice(path.indexOf('/') + 1), bucket: path.split('/')[0], size: 10, n_files: 1, mtime: null, created: null,
  last_read: null, cb: {}, keep: false, nested: false, check: null, check_n: null, check_eq: null, staged: null, ...o,
})
const group = (tier: 'tree' | 'moved', items: DupCopy[], o: Partial<DupGroup> = {}): DupGroup => ({
  group: 'g', tier, rel: 'x', kind: 'dir', size: 10, n_files: 1, copies: items.length, reclaimable: 10 * (items.length - 1),
  n_buckets: 2, suspect: false, items, ...o,
})
const DAY = 20_000

describe('idle', () => {
  it('counts from the last read, else from creation', () => {
    expect(idleDays({ last_read: DAY - 3, created: 0 }, DAY)).toBe(3)
    expect(idleDays({ last_read: null, created: (DAY - 40) * 86_400 + 5 }, DAY)).toBe(40)
    expect(idleDays({ last_read: null, created: null }, DAY)).toBe(null)
  })
  it("is a group's most recently touched copy's", () => {
    expect(groupIdle(group('tree', [copy('a/x', { last_read: DAY - 9 }), copy('b/x', { last_read: DAY - 2 }), copy('c/x')]), DAY)).toBe(2)
  })
})

describe('staging from /dups', () => {
  it('suggests every un-nested copy but the keep in a tree group', () => {
    const g = group('tree', [copy('a/x', { keep: true }), copy('b/x'), copy('c/x', { nested: true }), copy('d/x', { staged: 4 })])
    expect(suggested(g)).toEqual(['b/x'])
    expect(g.items.map(c => stageBlock(g, c))).toEqual([null, null, null, 'already staged (plan 4)'])
  })
  it('stages a moved copy only after a passing spot check, never one that differs', () => {
    const g = group('moved', [copy('a/x', { keep: true }), copy('b/y', { check: 'equal' }), copy('c/z', { check: 'partial' }), copy('d/w', { check: 'differs' }), copy('e/v')])
    expect(suggested(g)).toEqual(['b/y'])
    expect(g.items.map(c => stageBlock(g, c))).toEqual([
      'the suggested keep',
      null,
      'a moved copy needs a passing spot check first',
      "its objects differ from the keep copy's (spot check)",
      'a moved copy needs a passing spot check first',
    ])
  })
  it('stages a dir as its prefix and an object exactly', () => {
    expect(stageItems([{ path: 'a/x', kind: 'dir' }, { path: 'b/m.bin', kind: 'file' }], 'gs://')).toEqual([
      { key: 'gs://a/x/', kind: 'prefix' },
      { key: 'gs://b/m.bin', kind: 'object' },
    ])
  })
})

describe('sorting and the pair matrix', () => {
  const gs = [
    group('tree', [copy('a/1', { last_read: DAY - 1 })], { group: 'g1', size: 5 }),
    group('tree', [copy('a/2', { last_read: DAY - 50 })], { group: 'g2', size: 9 }),
    group('tree', [copy('a/3')], { group: 'g3', size: 9 }),
  ]
  it('sorts by size or idle time, keeping the report order on ties', () => {
    expect(sortGroups(gs, 'reclaim', DAY).map(g => g.group)).toEqual(['g1', 'g2', 'g3'])
    expect(sortGroups(gs, 'size', DAY).map(g => g.group)).toEqual(['g2', 'g3', 'g1'])
    expect(sortGroups(gs, 'idle', DAY).map(g => g.group)).toEqual(['g2', 'g1', 'g3'])
  })
  it("indexes one tier's pairs either way round", () => {
    const m = pairMatrix([
      { tier: 'tree', b1: 'a', b2: 'c', groups: 2, bytes: 7 },
      { tier: 'tree', b1: 'b', b2: 'b', groups: 1, bytes: 3 },
      { tier: 'moved', b1: 'a', b2: 'z', groups: 1, bytes: 1 },
    ], 'tree')
    expect(m.buckets).toEqual(['a', 'b', 'c'])
    expect([m.cell('c', 'a')?.bytes, m.cell('b', 'b')?.bytes, m.cell('a', 'b')]).toEqual([7, 3, undefined])
  })
})
