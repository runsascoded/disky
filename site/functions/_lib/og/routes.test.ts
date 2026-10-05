import { describe, expect, it } from 'vitest'
import { pageView } from './routes'

const pv = (p: string) => pageView(new URL(`https://site.example.org${p}`), 'marin GCS')

describe('pageView: page URL → card kind, view params, title', () => {
  it('map pages: the path, and only the params that change the card', () => {
    expect([
      pv('/'),
      pv('/marin-us-central2/checkpoints?d=261002&f=tomat&n=50&open=x'),
      pv('/marin-a/run%20one?o=unowned&c=age'),
    ]).toEqual([
      { kind: 'map', params: {}, title: 'marin GCS' },
      { kind: 'map', params: { path: 'marin-us-central2/checkpoints', d: '261002', f: 'tomat' }, title: 'marin-us-central2/checkpoints · filter: tomat' },
      { kind: 'map', params: { path: 'marin-a/run one', o: 'unowned', c: 'age' }, title: 'marin-a/run one · owner: unowned' },
    ])
  })
  it('the other pages', () => {
    expect([pv('/staged?q=hedy%7Cgrace&s=-o'), pv('/staged'), pv('/users'), pv('/user/alan-turing'), pv('/assignments')]).toEqual([
      { kind: 'staged', params: { q: 'hedy|grace' }, title: 'Staged for deletion: “hedy|grace”' },
      { kind: 'staged', params: {}, title: 'Staged for deletion' },
      { kind: 'users', params: {}, title: 'marin GCS — users' },
      { kind: 'user', params: { id: 'alan-turing' }, title: 'alan-turing · marin GCS' },
      { kind: 'assignments', params: {}, title: 'marin GCS — assigner × assignee' },
    ])
  })
  it('no card: API, assets, other pages; `..` is resolved by the URL parser first', () => {
    expect(['/api/subtree', '/og/map.png', '/files/listing', '/admin', '/og.jpg', '/assets/index.js', '/a/../b', '/a/%2E%2E/b'].map(pv))
      .toEqual([null, null, null, null, null, null, { kind: 'map', params: { path: 'b' }, title: 'b' }, { kind: 'map', params: { path: 'b' }, title: 'b' }])
  })
})
