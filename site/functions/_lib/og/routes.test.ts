import { describe, expect, it } from 'vitest'
import { pageView } from './routes'

const pv = (p: string) => pageView(new URL(`https://site.example.org${p}`), 'marin GCS')

describe('pageView: page URL → card kind, view params, title', () => {
  it('distinguishes the explicit diff section from a date range on the snapshot map', () => {
    expect([
      pv('/bucket?d=26092812-26091700#diff'),
      pv('/bucket?d=26092812-26091700&section=diff&dm=max'),
    ]).toEqual([
      { kind: 'map', params: { path: 'bucket', d: '26092812-26091700' }, title: 'bucket' },
      { kind: 'diff', params: { path: 'bucket', d: '26092812-26091700', dm: 'max' }, title: 'bucket · changes' },
    ])
  })
  it('map pages: the path, and only the params that change the card', () => {
    expect([
      pv('/'),
      pv('/marin-us-central2/checkpoints?d=261002&f=tomat&n=50&open=x'),
      pv('/marin-a/run%20one?o=unowned&c=age'),
      pv('/marin-a?o&d=261002'),
      pv('/marin-a?o=*'),
    ]).toEqual([
      { kind: 'map', params: {}, title: 'marin GCS' },
      { kind: 'map', params: { path: 'marin-us-central2/checkpoints', d: '261002', f: 'tomat' }, title: 'marin-us-central2/checkpoints · filter: tomat' },
      { kind: 'map', params: { path: 'marin-a/run one', o: 'unowned', c: 'age' }, title: 'marin-a/run one · owner: unowned' },
      { kind: 'map', params: { path: 'marin-a', o: '', d: '261002' }, title: 'marin-a · owner: unowned' },
      { kind: 'map', params: { path: 'marin-a', o: '*' }, title: 'marin-a · owner: owned' },
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

describe('every page route of the app has its own card or none, never a folder card', () => {
  // `src/Root.tsx`'s routes: an app page the parser took for a folder unfurled as an empty map of `/scans`, `/meta`, …
  // `node:fs` loaded dynamically: the Functions' tsconfig has the Workers types only (as `functions/routes.test.ts`).
  const rootRoutes = async (): Promise<string[]> => {
    const fs = 'node:fs'
    const { readFileSync } = (await import(/* @vite-ignore */ fs)) as { readFileSync(file: URL, enc: 'utf8'): string }
    const src = readFileSync(new URL('../../../src/Root.tsx', (import.meta as ImportMeta & { url: string }).url), 'utf8')
    return [...src.matchAll(/<Route path="([^"]+)"/g)].map(m => m[1]).filter(p => p !== '*')
  }
  it('Root.tsx routes → kinds', async () => {
    const routes = await rootRoutes()
    const kinds = Object.fromEntries(routes.map(r => [r, pv(r.replace(/:\w+/g, 'x').replace(/\*$/, 'x'))?.kind ?? null]))
    expect(Object.entries(kinds).filter(([, k]) => k === 'map')).toEqual([])
    expect(kinds['/staged']).toBe('staged')
    expect([kinds['/scans'], kinds['/scans/*'], kinds['/meta/*'], kinds['/names']]).toEqual([null, null, null, null])
  })
})

describe('the card view keys the scan by the canonical ?d= (legacy and ISO spellings fold in)', () => {
  it.each([
    ['/?d=261009', '261009'],
    ['/?d=2026-10-09', '261009'],
    ['/?d=2026-10-09T1200', '2610091200'],
    ['/?d=261009-1200', '2610091200'],
    ['/?d=26100912', '26100912'],
    ['/?date=2026-10-09', '261009'],
    ['/?date=2026-10-09&from=2026-10-05', '261009-261005'],
    ['/users?date=2026-10-09T0601', '2610090601'],
    ['/user/alice?d=2026-10-06', '261006'],
    ['/assignments?d=261009-12', '26100912'],
    // unparseable: kept verbatim, so the card is a miss (404), not the latest scan
    ['/?d=junk', 'junk'],
  ])('%s → d=%s', (path, d) => {
    expect(pv(path)?.params.d).toBe(d)
  })
})
