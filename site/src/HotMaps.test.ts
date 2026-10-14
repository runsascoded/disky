import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TreemapProps } from '@rdub/treemap'
import { HotMaps } from './HotMaps'
import { HotTotals } from './HotSearch'
import type { HotMapNode } from './hotTreemap'
import type { HotView } from './hotModel'

const { maps } = vi.hoisted(() => ({ maps: [] as TreemapProps<HotMapNode>[] }))
vi.mock('./SiteKbd', () => ({ SiteKbd: () => null }))
vi.mock('@rdub/treemap', async importOriginal => ({ ...await importOriginal<typeof import('@rdub/treemap')>(),
  Treemap: (props: TreemapProps<HotMapNode>) => { maps.push(props); return createElement('div', { className: 'fixture-map' }) },
}))

function fixture(date = '2026-10-05'): HotView {
  return { target: 'fixture', date, pattern: '.json', root: { b: 12, o: 9 }, buckets: [
    { path: 'bucket-a', pre: 1, post: 4, b: 7, o: 2 }, { path: 'bucket-b', pre: 5, post: 8, b: 5, o: 3 },
    { path: 'bucket-c', pre: 9, post: 12, b: 0, o: 4 }, { path: 'bucket-d', pre: 13, post: 16, b: 0, o: 0 },
    { path: 'bucket-e', pre: 17, post: 20, b: 0, o: 0 }, { path: 'bucket-f', pre: 21, post: 24, b: 0, o: 0 },
  ] }
}
beforeEach(() => { maps.length = 0 })

describe('registered L1 snapshot maps', () => {
  it('renders one dated map with byte-only renderer options and no drill/link/native tooltip controls', () => {
    const html = renderToStaticMarkup(createElement(HotMaps, { result: { after: fixture() } }))
    expect([...html.matchAll(/<figcaption>(.*?)<\/figcaption>/g)].map(([, text]) => text))
      .toEqual(['2026-10-05<span>12 B matching bytes</span>'])
    expect([...html.matchAll(/<h2>(.*?)<\/h2>|<p>(.*?)<\/p>/g)].map(([, heading, paragraph]) => heading ?? paragraph))
      .toEqual(['Matching bytes by bucket', '“.json” on 2026-10-05'])
    expect(maps.map(props => ({ renderer: props.renderer, chrome: props.chrome, fullscreen: props.fullscreen,
      tiling: props.tiling, minCellArea: props.minCellArea, minCellSide: props.minCellSide,
      cellHref: props.cellHref, loadChildren: props.loadChildren, hasChildren: props.hasChildren,
      children: props.getChildren(props.root), size: props.getSize(props.root), format: props.formatSize!(12),
      tooltip: props.renderTooltip!(props.root, [props.root]),
    }))).toEqual([{ renderer: 'dom', chrome: false, fullscreen: false, tiling: 'shared',
      minCellArea: null, minCellSide: null, cellHref: undefined, loadChildren: undefined, hasChildren: undefined,
      children: [{ path: 'bucket-a', b: 7, o: 2, color: '#3c8cdd' }, { path: 'bucket-b', b: 5, o: 3, color: '#e88c30' }],
      size: 12, format: '12 B', tooltip: null,
    }])
    expect(maps[0].onCellClick!(maps[0].root.children![0], [maps[0].root], {} as never)).toBe(true)
    expect([...html.matchAll(/<(a|button)\b/g)].map(([, tag]) => tag)).toEqual([])
  })
  it('renders equal-container before/after maps using separate positive snapshot sizes, not signed delta', () => {
    const before = fixture('2026-10-04'), after = fixture()
    after.root.b = 20
    after.buckets[0].b = 1
    after.buckets[1].b = 19
    const html = renderToStaticMarkup(createElement(HotMaps, { result: { before, after, delta: { b: 8, o: 0 } } }))
    expect([...html.matchAll(/<figcaption>(.*?)<\/figcaption>/g)].map(([, text]) => text))
      .toEqual(['2026-10-04<span>12 B matching bytes</span>', '2026-10-05<span>20 B matching bytes</span>'])
    expect(html.match(/<div class="hot-map-pair[^"]*">/)?.[0]).toBe('<div class="hot-map-pair comparison">')
    expect(maps.map(props => ({ b: props.getSize(props.root), areas: props.root.children!.map(child => props.getSize(child)),
      colors: props.root.children!.map(child => child.color) }))).toEqual([
      { b: 12, areas: [7, 5], colors: ['#3c8cdd', '#e88c30'] },
      { b: 20, areas: [1, 19], colors: ['#3c8cdd', '#e88c30'] },
    ])
  })
  it.each([9, 0])('does not draw a fake map for %s zero-byte objects and keeps exact table counts', objects => {
    const after = fixture()
    after.root = { b: 0, o: objects }
    after.buckets.forEach(row => { row.b = 0; row.o = 0 })
    after.buckets[2].o = objects
    const html = renderToStaticMarkup(createElement(HotMaps, { result: { after } }))
    expect(maps).toEqual([])
    expect([...html.matchAll(/<div class="hot-map-empty">(.*?)<\/div>/g)].map(([, text]) => text))
      .toEqual([objects ? '9 matching objects total 0 bytes, so no byte area is drawn. Exact counts remain in the table.'
        : 'No matching bytes or objects in this snapshot.'])
    const table = renderToStaticMarkup(createElement(HotTotals, { result: { after } }))
    expect([...table.matchAll(/<tr(?: class="hot-fleet")?><th scope="row">(.*?)<\/th><td>(.*?)<\/td><td>(.*?)<\/td><\/tr>/g)]
      .map(([, path, bytes, count]) => [path, bytes, count])).toEqual([
      ['All buckets', '0', String(objects)], ['bucket-a', '0', '0'], ['bucket-b', '0', '0'],
      ['bucket-c', '0', String(objects)], ['bucket-d', '0', '0'], ['bucket-e', '0', '0'], ['bucket-f', '0', '0'],
    ])
  })
})
