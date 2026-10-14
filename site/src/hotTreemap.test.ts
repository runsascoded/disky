import { squarify } from '@rdub/treemap'
import { describe, expect, it } from 'vitest'
import type { HotView } from './hotModel'
import { hotMap } from './hotTreemap'

export function mapFixture(date = '2026-10-05'): HotView {
  return { target: 'fixture', date, pattern: '.json', root: { b: 12, o: 9 }, buckets: [
    { path: 'bucket-a', pre: 1, post: 4, b: 7, o: 2 },
    { path: 'bucket-b', pre: 5, post: 8, b: 5, o: 3 },
    { path: 'bucket-c', pre: 9, post: 12, b: 0, o: 4 },
    { path: 'bucket-d', pre: 13, post: 16, b: 0, o: 0 },
    { path: 'bucket-e', pre: 17, post: 20, b: 0, o: 0 },
    { path: 'bucket-f', pre: 21, post: 24, b: 0, o: 0 },
  ] }
}

describe('byte-only bucket treemap adapter', () => {
  it('retains exact root weights and positive bytes without fabricating zero-byte area', () => {
    const view = mapFixture()
    const original = structuredClone(view)
    expect(hotMap(view)).toEqual({ date: '2026-10-05', pattern: '.json', empty: null, root: {
      path: 'All buckets', b: 12, o: 9, children: [
        { path: 'bucket-a', b: 7, o: 2, color: '#3c8cdd' },
        { path: 'bucket-b', b: 5, o: 3, color: '#e88c30' },
      ],
    } })
    expect(view).toEqual(original)
    const rects = squarify(hotMap(view).root.children!, 0, 0, 120, 100, row => row.b)
    expect(rects.map(({ it, w, h }) => [it.path, w * h])).toEqual([['bucket-a', 7000], ['bucket-b', 5000]])
  })
  it('uses stable logical path order/colors while each snapshot owns its area weights', () => {
    const before = mapFixture('2026-10-04'), after = mapFixture()
    after.buckets[0].b = 3
    after.buckets[1].b = 9
    after.buckets.reverse()
    expect([hotMap(before), hotMap(after)].map(snapshot => snapshot.root.children))
      .toEqual([
        [{ path: 'bucket-a', b: 7, o: 2, color: '#3c8cdd' }, { path: 'bucket-b', b: 5, o: 3, color: '#e88c30' }],
        [{ path: 'bucket-a', b: 3, o: 2, color: '#3c8cdd' }, { path: 'bucket-b', b: 9, o: 3, color: '#e88c30' }],
      ])
  })
  it('does not shift colors when a bucket has zero bytes on only one date', () => {
    const view = mapFixture()
    view.buckets[0].b = 0
    view.root.b = 5
    expect(hotMap(view).root.children).toEqual([{ path: 'bucket-b', b: 5, o: 3, color: '#e88c30' }])
  })
  it('keeps path colors identical across complete independent preorder domains', () => {
    const before = mapFixture('2026-10-05'), after = mapFixture('2026-10-06')
    before.buckets.forEach(row => { row.b = 1 }); before.root.b = 6
    after.buckets.reverse()
    after.buckets.forEach((row, i) => { row.pre = 1 + 3 * i; row.post = 3 + 3 * i; row.b = 1 }); after.root.b = 6
    const colors = (view: HotView) => hotMap(view).root.children!.map(row => [row.path, row.color] as const).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    const expected = [
      ['bucket-a', '#3c8cdd'], ['bucket-b', '#e88c30'], ['bucket-c', '#34b288'], ['bucket-d', '#d7425b'],
      ['bucket-e', 'hsl(280 55% 55%)'], ['bucket-f', 'hsl(50 75% 55%)'],
    ]
    expect(colors(before)).toEqual(expected)
    expect(colors(after)).toEqual(expected)
  })
  it('retains all six palette slots in fixed logical path order', () => {
    const view = mapFixture()
    view.buckets.forEach(row => { row.b = 1 })
    view.root.b = 6
    expect(hotMap(view).root.children!.map(row => [row.path, row.color])).toEqual([
      ['bucket-a', '#3c8cdd'], ['bucket-b', '#e88c30'], ['bucket-c', '#34b288'], ['bucket-d', '#d7425b'],
      ['bucket-e', 'hsl(280 55% 55%)'], ['bucket-f', 'hsl(50 75% 55%)'],
    ])
  })
  it.each([9, 0])('distinguishes %s zero-byte objects from no matching objects', objects => {
    const view = mapFixture()
    view.root = { b: 0, o: objects }
    view.buckets.forEach(row => { row.b = 0; row.o = 0 })
    view.buckets[2].o = objects
    expect(hotMap(view)).toEqual({ date: '2026-10-05', pattern: '.json',
      root: { path: 'All buckets', b: 0, o: objects, children: [] },
      empty: objects ? '9 matching objects total 0 bytes, so no byte area is drawn. Exact counts remain in the table.'
        : 'No matching bytes or objects in this snapshot.',
    })
  })
})
