import { describe, expect, it } from 'vitest'
import { pageView, shellPage, storeOfPath, urlName } from './routes'

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
  it('the section the scroll-spy names (`?section=`): diff and over-time get their cards, the rest the map; `?s=` is the shade axis', () => {
    expect([
      pv('/bucket?s=c&section=diff'),
      pv('/bucket?section=over-time'),
      pv('/bucket?section=tbl&d=261002'),
      pv('/bucket?section=mtime&c=age'),
      pv('/bucket?section=tree-map'),
      pv('/bucket?s=diff'),
      pv('/bucket?s=over-time'),
    ]).toEqual([
      { kind: 'diff', params: { path: 'bucket' }, title: 'bucket · changes' },
      { kind: 'series', params: { path: 'bucket' }, title: 'bucket · size over time' },
      { kind: 'map', params: { path: 'bucket', d: '261002' }, title: 'bucket' },
      { kind: 'map', params: { path: 'bucket', c: 'age' }, title: 'bucket' },
      { kind: 'map', params: { path: 'bucket' }, title: 'bucket' },
      { kind: 'map', params: { path: 'bucket' }, title: 'bucket' },
      { kind: 'map', params: { path: 'bucket' }, title: 'bucket' },
    ])
  })
  it('the size-over-time view: the path and the scope the series follows (`d f qs o by cl`), not the colour axis', () => {
    expect([
      pv('/?section=over-time'),
      pv('/marin-a/ckpts?section=over-time&d=261002&f=tomat&qs=g&o=alan&by=bob&cl=hot&c=age&dm=max&n=50'),
      pv('/marin-a?section=over-time&o'),
      pv('/marin-a?section=over-time&o=*'),
    ]).toEqual([
      { kind: 'series', params: {}, title: 'marin GCS · size over time' },
      { kind: 'series', params: { path: 'marin-a/ckpts', d: '261002', f: 'tomat', qs: 'g', o: 'alan', by: 'bob', cl: 'hot' }, title: 'marin-a/ckpts · size over time · filter: tomat · owner: alan' },
      { kind: 'series', params: { path: 'marin-a', o: '' }, title: 'marin-a · size over time · owner: unowned' },
      { kind: 'series', params: { path: 'marin-a', o: '*' }, title: 'marin-a · size over time · owner: owned' },
    ])
  })
  it('`?v=` names (and the one-letter codes) name the same views as the legacy `?section=` ids, and a spelled-out `?v=` id is read too', () => {
    expect([pv('/bucket?d=26092812-26091700&v=diff&dm=max'), pv('/bucket?v=time'), pv('/bucket?v=t&d=261002'), pv('/bucket?v=over-time'), pv('/bucket?v=o&section=diff')]).toEqual([
      { kind: 'diff', params: { path: 'bucket', d: '26092812-26091700', dm: 'max' }, title: 'bucket · changes' },
      { kind: 'series', params: { path: 'bucket' }, title: 'bucket · size over time' },
      { kind: 'map', params: { path: 'bucket', d: '261002' }, title: 'bucket' },
      { kind: 'series', params: { path: 'bucket' }, title: 'bucket · size over time' },
      { kind: 'series', params: { path: 'bucket' }, title: 'bucket · size over time' },
    ])
  })
  it('a secondary store\'s over-time section carries its `store`', () => {
    expect(pageView(new URL('https://site.example.org/snapshots?section=over-time&d=261012'), 'CW — meta', [], 'meta'))
      .toEqual({ kind: 'series', params: { store: 'meta', path: 'snapshots', d: '261012' }, title: 'snapshots · size over time' })
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
    expect([kinds['/scans'], kinds['/scans/*'], kinds['/meta/*'], kinds['/names'], kinds['/dups']]).toEqual([null, null, null, null, null])
  })
  it('every route with no card kind is a named page, or says why not', async () => {
    const routes = (await rootRoutes()).filter(r => pv(r.replace(/:\w+/g, 'x').replace(/\*$/, 'x')) === null)
    const pages = Object.fromEntries(routes.map(r => [r, shellPage(r.replace(/:\w+/g, 'x').replace(/\*$/, 'x'))?.page ?? null]))
    expect(pages).toEqual({
      '/signin': 'signin', '/privacy': 'privacy',
      '/admin': 'admin', '/admin/db': 'admin', '/admin/db/:table': 'admin', '/admin/sessions': 'admin', '/admin/latency': 'admin', '/admin/activity': 'admin',
      '/admin/usage': 'admin', '/admin/slack': 'admin', '/admin/cost/:cloud': 'admin', '/admin/sessions/:sid': 'admin',
      '/files/*': 'files', '/scans': 'scans', '/scans/*': 'scan', '/health': 'health', '/names': 'names', '/dups': 'dups',
      '/sweep': 'staged', '/hot': 'names', '/marks': 'map', '/mark': 'map',
      // the dev previews, the OG render pages, a redirect to a map, a redirect to the root, and `functions/runs`' own stamp
      '/dev/federated-scans': null, '/dev/children-table': null, '/dev/scan-durations': null, '/users/og': null, '/user/:id/og': null, '/meta/*': null, '/users/*': null, '/runs/*': null,
    })
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

describe('pageView: a mounted store (`STORE_ROOT`)', () => {
  const pvm = (p: string) => pageView(new URL(`https://site.example.org${p}`), 'Open Athena Hugging Face', ['open-athena'])
  it('reads its URLs below the root, titled without it; a link from before the mount reads the same', () => {
    expect([pvm('/'), pvm('/models/m?d=261018'), pvm('/open-athena/models/m?d=261018')]).toEqual([
      { kind: 'map', params: { path: 'open-athena' }, title: 'Open Athena Hugging Face' },
      { kind: 'map', params: { path: 'open-athena/models/m', d: '261018' }, title: 'models/m' },
      { kind: 'map', params: { path: 'open-athena/models/m', d: '261018' }, title: 'models/m' },
    ])
  })
})

describe('storeOfPath: a page URL below a secondary store\'s `path` is that store\'s', () => {
  const STORES = [{ key: 'meta', path: '/meta' }, { key: 'nas', path: '/nas' }, { key: 'nas-tv', path: '/nas/tv' }]
  it('the longest matching path, and the pathname below it; anything else is the primary\'s', () => {
    expect(['/meta', '/meta/', '/meta/snapshots/x', '/nas/media', '/nas/tv/shows', '/metadata', '/', '/a/meta'].map(p => storeOfPath(p, STORES))).toEqual([
      { key: 'meta', rest: '/' },
      { key: 'meta', rest: '/' },
      { key: 'meta', rest: '/snapshots/x' },
      { key: 'nas', rest: '/media' },
      { key: 'nas-tv', rest: '/shows' },
      null,
      null,
      null,
    ])
    expect(storeOfPath('/meta/x', [])).toBe(null)
  })
})

describe('pageView: a secondary store\'s page (below its path)', () => {
  const pvs = (p: string, site = 'Marin GCS — meta', root: string[] = []) => pageView(new URL(`https://site.example.org${p}`), site, root, 'meta')
  it('map and diff pages, titled by its label at its root; the view carries `store`', () => {
    expect([
      pvs('/'),
      pvs('/snapshots/meta?d=261018&f=json&n=50'),
      pvs('/snapshots?d=261018-261017&section=diff'),
    ]).toEqual([
      { kind: 'map', params: { store: 'meta' }, title: 'Marin GCS — meta' },
      { kind: 'map', params: { store: 'meta', path: 'snapshots/meta', d: '261018', f: 'json' }, title: 'snapshots/meta · filter: json' },
      { kind: 'diff', params: { store: 'meta', path: 'snapshots', d: '261018-261017' }, title: 'snapshots · changes' },
    ])
  })
  it('its paths are its own: the primary\'s reserved and file-looking names are folders here; `files`, `scans` and `og` have no card', () => {
    expect([pvs('/staged'), pvs('/users'), pvs('/scans.json'), pvs('/files/x'), pvs('/scans'), pvs('/scans/uid'), pvs('/og')]).toEqual([
      { kind: 'map', params: { store: 'meta', path: 'staged' }, title: 'staged' },
      { kind: 'map', params: { store: 'meta', path: 'users' }, title: 'users' },
      { kind: 'map', params: { store: 'meta', path: 'scans.json' }, title: 'scans.json' },
      null,
      null,
      null,
      null,
    ])
  })
  it('its own mount (`STORE_ROOT`): left out of its URLs and titles, leading its paths', () => {
    expect([pvs('/', 'nas', ['mnt', 'user']), pvs('/media/tv', 'nas', ['mnt', 'user']), pvs('/mnt/user/media', 'nas', ['mnt', 'user'])]).toEqual([
      { kind: 'map', params: { store: 'meta', path: 'mnt/user' }, title: 'nas' },
      { kind: 'map', params: { store: 'meta', path: 'mnt/user/media/tv' }, title: 'media/tv' },
      { kind: 'map', params: { store: 'meta', path: 'mnt/user/media' }, title: 'media' },
    ])
  })
})

describe('shellPage: below a secondary store\'s path, only its object opener and scan runs', () => {
  it('its other paths are its map; a bad escape has no id', () => {
    expect([shellPage('/scans', true), shellPage('/scans/a/b', true), shellPage('/files/x', true), shellPage('/admin', true), shellPage('/dups', true), shellPage('/scans/%E0%A4%A')]).toEqual([
      { page: 'scans' }, { page: 'scan', id: 'a/b' }, { page: 'files' }, null, null, { page: 'scan' },
    ])
  })
})

describe('urlName: what the URL itself calls a page', () => {
  it('a map page\'s path and section, a user\'s id, or nothing', () => {
    expect(['/', '/x/b?f=x&o=y', '/x?section=diff', '/?section=over-time', '/user/alan', '/users', '/staged'].map(p => urlName(pv(p)!))).toEqual([
      null, 'x/b', 'x · changes', 'size over time', 'alan', null, null,
    ])
    const mounted = (p: string) => pageView(new URL(`https://site.example.org${p}`), 'hf', ['oa'])!
    expect(['/', '/models/m'].map(p => urlName(mounted(p), ['oa']))).toEqual([null, 'models/m'])
  })
})
