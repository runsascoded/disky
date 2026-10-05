import { describe, expect, it } from 'vitest'
import { relAgo, sortPrefixRows } from './prefixes'

describe('relAgo', () => {
  it('each unit once its count is unambiguous', () => {
    const now = 1_000_000_000
    const at = (s: number) => relAgo(now - s, now)
    const D = 86400
    expect([at(5), at(90), at(3 * 3600), at(3 * D), at(13 * D), at(14 * D), at(60 * D), at(61 * D), at(400 * D), at(729 * D), at(730 * D), at(-10)]).toEqual([
      '5s ago', '1m ago', '3h ago', '3d ago', '13d ago', '2w ago', '8w ago', '2mo ago', '13mo ago', '23mo ago', '1y ago', '0s ago',
    ])
  })
})

describe('sortPrefixRows', () => {
  const rows = [
    { name: 'gs://b/a/', staged: 3, stat: { b: 10, o: 1 } },
    { name: 'gs://b/b/', staged: 1 },
    { name: 'gs://b/c/', staged: 2, stat: { b: 30, o: 1, d: 5 } },
    { name: 'gs://b/d/', staged: 2, stat: { b: 10, o: 2 } },
  ]
  const order = (k: string, asc: boolean) => sortPrefixRows(rows, k, asc, r => r.staged).map(r => r.name.slice(7, 8))
  it('by bytes, created, an extra column; rows without the value last in both directions; ties by name', () => {
    expect([order('b', false), order('b', true), order('d', false), order('staged', true), order('name', false)]).toEqual([
      ['c', 'a', 'd', 'b'],
      ['a', 'd', 'c', 'b'],
      ['c', 'a', 'b', 'd'],
      ['b', 'c', 'd', 'a'],
      ['d', 'c', 'b', 'a'],
    ])
  })
})
