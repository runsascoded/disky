import { describe, expect, it } from 'vitest'
import { pageOfIndex, pageParam, pageWindow } from './Pager'

describe('pageWindow: the page of a list, clamped', () => {
  it('names the page, the page count and the half-open row range', () => {
    expect(pageWindow(5404, 50, 0)).toEqual({ page: 0, pages: 109, from: 0, to: 50 })
    expect(pageWindow(5404, 50, 108)).toEqual({ page: 108, pages: 109, from: 5400, to: 5404 })
  })
  it('clamps a page past either end (a list that shrank under its page)', () => {
    expect(pageWindow(120, 50, 9)).toEqual({ page: 2, pages: 3, from: 100, to: 120 })
    expect(pageWindow(120, 50, -1)).toEqual({ page: 0, pages: 3, from: 0, to: 50 })
  })
  it('an empty list is one empty page', () => {
    expect(pageWindow(0, 50, 0)).toEqual({ page: 0, pages: 1, from: 0, to: 0 })
  })
  it('walking every page shows every row exactly once, in order', () => {
    const rows = Array.from({ length: 5404 }, (_, i) => i)
    const { pages } = pageWindow(rows.length, 50, 0)
    const seen = Array.from({ length: pages }, (_, p) => {
      const { from, to } = pageWindow(rows.length, 50, p)
      return rows.slice(from, to)
    }).flat()
    expect(seen).toEqual(rows)
  })
})

describe('pageOfIndex: the page holding a row', () => {
  it('finds a linked row across a page boundary', () => {
    expect([0, 49, 50, 5403].map(i => pageOfIndex(i, 50))).toEqual([0, 0, 1, 108])
  })
})

describe('pageParam: a list\'s page in the URL', () => {
  it('is 1-based, absent on the first page, and reads anything else as the first', () => {
    expect([0, 1, 108].map(p => pageParam.encode(p))).toEqual([undefined, '2', '109'])
    expect([undefined, '', '1', '2', '109', '0', '-3', '2.5', 'x'].map(s => pageParam.decode(s))).toEqual([0, 0, 0, 1, 108, 0, 0, 0, 0])
  })
})
