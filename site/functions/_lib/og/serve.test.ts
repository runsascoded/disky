import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cardSvg, type CardData } from './card'
import { expDay, imagePath, ogKey } from './sign'
import { anonymousPage, pageOf, serveCard, siteOf, stampPage, type OgEnv } from './serve'
import { anonymousPolicy, genericTitle } from './policy'
import { fullTier, mint } from './tokens'
import { EPOCH } from './sign'
import { sqliteD1 } from '../testD1'

// 2026-10-02T12:00Z: day 274.
const NOW = EPOCH + 274 * 86400 + 12 * 3600

const mocks = vi.hoisted(() => ({
  data: vi.fn(), scan: vi.fn(), head: vi.fn(), warm: vi.fn(), live: vi.fn(), tier: vi.fn(),
  stamp: vi.fn(), render: vi.fn(), match: vi.fn(), put: vi.fn(), diff: vi.fn(), pair: vi.fn(),
}))
vi.mock('./vendor/resvg.wasm', () => ({ default: new Uint8Array() }))
vi.mock('./render', () => ({ ensureWasm: vi.fn(), FONT_FILES: [], svgToPng: mocks.render }))
vi.mock('./data', () => ({
  mapCard: mocks.data, resolveScan: mocks.scan,
  NoScanMatch: class NoScanMatch extends Error {},
}))
vi.mock('./pages', () => ({ assignmentsCard: mocks.data, stagedCard: mocks.data, userCard: mocks.data, usersCard: mocks.data }))
vi.mock('./diff', () => ({ diffCard: mocks.diff, resolveDiffScans: mocks.pair }))
vi.mock('../ledger', () => ({ ledgerHead: mocks.head }))
vi.mock('../identity', () => ({ canonId: (s: string) => s, loadRegistry: vi.fn().mockResolvedValue({}) }))
vi.mock('../../api/subtree', () => ({ warmSubtree: mocks.warm }))
vi.mock('../unfurl', () => ({ stampMeta: mocks.stamp }))
vi.mock('./tier', () => ({ credLive: mocks.live, pageTier: mocks.tier }))

const protectedData: CardData = {
  tier: 'full', site: 'CW storage', title: 'secret/path', subtitle: 'scan 2026-10-12T1201',
  total: '99 TiB · 123 objects', tiles: [{ name: 'secret', b: 99, color: '#4269d0' }],
}
const env = (extra: Partial<OgEnv> = {}): OgEnv => ({
  ROOT_LABEL: 'CW storage', SESSION_SECRET: 'test-only-secret',
  ASSETS: { fetch: async () => new Response('<html></html>', { headers: { 'content-type': 'text/html' } }) },
  ...extra,
})
const request = (path: string) => new Request(`https://cw.example${path}`)
const textNodes = (svg: string) => [...svg.matchAll(/<text\b[^>]*>(.*?)<\/text>/g)].map(m => m[1])

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('caches', { default: { match: mocks.match, put: mocks.put } })
  mocks.match.mockResolvedValue(undefined)
  mocks.put.mockResolvedValue(undefined)
  mocks.data.mockResolvedValue(protectedData)
  mocks.scan.mockResolvedValue('2026-10-12T1201')
  mocks.pair.mockResolvedValue({ from: '2026-10-10T1201', to: '2026-10-12T1201' })
  mocks.diff.mockResolvedValue(protectedData)
  mocks.head.mockResolvedValue(10)
  mocks.live.mockResolvedValue(false)
  mocks.tier.mockResolvedValue({ tier: 'anon', cred: null })
  mocks.stamp.mockImplementation((html: Response) => html)
  mocks.render.mockImplementation((svg: string) => new TextEncoder().encode(svg))
})

describe('restricted anonymous OG responses', () => {
  it('diff HTML pins both scans and describes the comparison; the image validates the original view', async () => {
    mocks.tier.mockResolvedValue({ tier: 'full', day: expDay(Math.floor(Date.now() / 1000), 7), cred: { t: 'DiffTokAAA' } })
    mocks.live.mockResolvedValue(true)
    const url = new URL('https://cw.example/bucket?section=diff&d=-7d&og=DiffTokAAA')
    await stampPage(env({ OG_CARDS: '1' }), url, new Response('shell'))
    const meta = mocks.stamp.mock.calls[0][1]
    const image = new URL(meta.image)
    expect([meta.title, meta.desc, image.pathname, image.searchParams.get('s'), image.searchParams.get('b')])
      .toEqual(['secret/path', 'scan 2026-10-12T1201 · 99 TiB · 123 objects', '/og/diff.png', '2026-10-12T1201', '2026-10-10T1201'])
    await serveCard({ request: new Request(image), env: env({ OG_CARDS: '1' }) })
    expect(mocks.live.mock.calls.map(call => [call[1], call[2], call[3]])).toEqual([
      ['diff', { d: '-7d', path: 'bucket' }, { t: 'DiffTokAAA' }],
    ])
    expect(mocks.diff.mock.calls.map(call => [call[3], call[4], call[5]])).toEqual([
      [{ path: 'bucket', d: '2610121201-2610101201' }, 'full', true],
      [{ path: 'bucket', d: '2610121201-2610101201' }, 'full', undefined],
    ])
    expect(mocks.pair.mock.calls.length).toBe(1)
  })

  it('a revoked warmed diff downgrades before any comparison reads', async () => {
    const key = await ogKey('test-only-secret')
    const path = await imagePath(key, 'diff', { path: 'secret', d: '-7d', s: '2026-10-12T1201', b: '2026-10-10T1201', t: 'DiffTokAAA' }, 'full', expDay(Math.floor(Date.now() / 1000), 7))
    const response = await serveCard({ request: request(path), env: env() })
    expect([textNodes(await response.text()), mocks.pair.mock.calls, mocks.diff.mock.calls])
      .toEqual([['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', ''], [], []])
  })
  it.each([
    '/og/map.png?path=secret/path&f=private&d=missing',
    '/og/staged.png?q=private',
    '/og/users.png',
    '/og/user.png?id=private',
    '/og/assignments.png',
    '/og/diff.png?path=secret&d=missing&sig=bad',
    '/og/map.png?path=nonexistent&sig=bad',
  ])('returns the same generic card without protected reads: %s', async path => {
    const waitUntil = vi.fn()
    const response = await serveCard({ request: request(path), env: env(), waitUntil })
    expect({ status: response.status, tier: response.headers.get('x-og-tier'), cache: response.headers.get('cache-control') })
      .toEqual({ status: 200, tier: 'anon', cache: 'no-store' })
    expect(textNodes(await response.text())).toEqual(['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', ''])
    expect([mocks.data.mock.calls, mocks.scan.mock.calls, mocks.head.mock.calls, mocks.warm.mock.calls, waitUntil.mock.calls, mocks.diff.mock.calls, mocks.pair.mock.calls]).toEqual([[], [], [], [], [], [], []])
  })

  it('a revoked, previously cached detailed card downgrades before accessing its cache', async () => {
    const k = await ogKey('test-only-secret')
    const path = await imagePath(k, 'map', { path: 'secret', t: 'TokenAAAAA' }, 'full', expDay(Math.floor(Date.now() / 1000), 7))
    mocks.match.mockImplementation(async (key: Request) => key.url.endsWith('tier=full&h=10') ? new Response('old protected card') : undefined)
    const response = await serveCard({ request: request(path), env: env() })
    expect(textNodes(await response.text())).toEqual(['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', ''])
    expect([mocks.live.mock.calls.length, mocks.data.mock.calls, mocks.head.mock.calls]).toEqual([1, [], []])
    expect(mocks.match.mock.calls.map(call => call[0].url)).toEqual(['https://cw.example/og/map.png?policy=generic&r=8'])
  })

  it('valid explicit shares remain detailed and private', async () => {
    mocks.live.mockResolvedValue(true)
    const k = await ogKey('test-only-secret')
    const path = await imagePath(k, 'map', { path: 'secret', t: 'TokenAAAAA' }, 'full', expDay(Math.floor(Date.now() / 1000), 7))
    const response = await serveCard({ request: request(path), env: env() })
    expect([response.status, response.headers.get('x-og-tier'), response.headers.get('cache-control')]).toEqual([200, 'full', 'private, no-store'])
    expect(textNodes(await response.text())).toEqual(textNodes(cardSvg(protectedData)))
    expect(mocks.data.mock.calls.map(call => [call[1], call[2], call[3], call[4]])).toEqual([
      [{ name: 'CW storage', scheme: 'gs://' }, 'secret', { path: 'secret', d: '2026-10-12T1201' }, 'full'],
    ])
  })

  it('anonymous HTML is generic even when dynamic metadata is disabled', async () => {
    await stampPage(env(), new URL('https://cw.example/secret/path?f=private'), new Response('shell'))
    expect(mocks.stamp.mock.calls.map(call => call[1])).toEqual([{
      title: 'disky — CW storage', desc: 'Private storage dashboard. Sign in to explore storage.',
      image: 'https://cw.example/og/map.png?policy=generic&r=8', imageType: 'image/png', page: 'https://cw.example/secret/path',
    }])
    expect([mocks.data.mock.calls, mocks.scan.mock.calls, mocks.head.mock.calls]).toEqual([[], [], []])
  })

  it('latest-scan detailed image URLs change across scans while the shared view stays unchanged', async () => {
    mocks.tier.mockResolvedValue({ tier: 'full', day: expDay(Math.floor(Date.now() / 1000), 7), cred: { t: 'TokenAAAAA' } })
    const url = new URL('https://cw.example/secret?og=TokenAAAAA')
    await stampPage(env({ OG_CARDS: '1' }), url, new Response('shell'))
    mocks.scan.mockResolvedValue('2026-10-13T1201')
    await stampPage(env({ OG_CARDS: '1' }), url, new Response('shell'))
    expect(mocks.stamp.mock.calls.map(call => {
      const image = new URL(call[1].image)
      return [image.searchParams.get('path'), image.searchParams.get('s'), image.searchParams.get('policy'), image.searchParams.get('r')]
    })).toEqual([
      ['secret', '2026-10-12T1201', 'generic', '8'],
      ['secret', '2026-10-13T1201', 'generic', '8'],
    ])
  })

  it('a versioned detailed image renders its pinned scan and validates the original credential view', async () => {
    mocks.live.mockResolvedValue(true)
    const k = await ogKey('test-only-secret')
    const path = await imagePath(k, 'map', { path: 'secret', t: 'TokenAAAAA', s: '2026-10-11T1201', r: '2', policy: 'generic' }, 'full', expDay(Math.floor(Date.now() / 1000), 7))
    await serveCard({ request: request(path), env: env() })
    expect([
      mocks.live.mock.calls.map(call => [call[1], call[2], call[3]]),
      mocks.data.mock.calls.map(call => call[3]),
      mocks.scan.mock.calls,
    ]).toEqual([
      [['map', { path: 'secret' }, { t: 'TokenAAAAA' }]],
      [{ path: 'secret', d: '2026-10-11T1201' }],
      [],
    ])
  })

  it('anonymous route interception reads only the static shell, not page-specific data', async () => {
    const assets = vi.fn(async (_request: Request) => new Response('safe shell'))
    const response = await anonymousPage(env({ ASSETS: { fetch: assets } }), new URL('https://cw.example/staged?q=secret'))
    expect([await response!.text(), response!.headers.get('cache-control'), assets.mock.calls.map(call => call[0].url)])
      .toEqual(['safe shell', 'private, no-store', ['https://cw.example/']])
    expect([mocks.data.mock.calls, mocks.scan.mock.calls, mocks.head.mock.calls]).toEqual([[], [], []])
  })

  it('a valid explicit share bypasses anonymous route interception', async () => {
    mocks.tier.mockResolvedValue({ tier: 'full', cred: { t: 'TokenAAAAA' } })
    const assets = vi.fn()
    expect(await anonymousPage(env({ ASSETS: { fetch: assets } }), new URL('https://cw.example/staged?og=TokenAAAAA'))).toBeNull()
    expect(assets.mock.calls).toEqual([])
  })

  it('expired signatures cannot reach detailed caches or protected reads', async () => {
    const k = await ogKey('test-only-secret')
    const path = await imagePath(k, 'map', { path: 'secret', t: 'TokenAAAAA' }, 'full', expDay(Math.floor(Date.now() / 1000), -2))
    const response = await serveCard({ request: request(path), env: env() })
    expect([response.headers.get('x-og-tier'), textNodes(await response.text()), mocks.live.mock.calls, mocks.data.mock.calls])
      .toEqual(['anon', ['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', ''], [], []])
  })

  it('explicit shape policy retains approved anonymous totals, with scan-versioned cache keys', async () => {
    mocks.data.mockResolvedValue({ ...protectedData, tier: 'anon' })
    const response = await serveCard({ request: request('/og/map.png'), env: env({ OG_ANONYMOUS: 'shape' }) })
    expect([response.headers.get('x-og-tier'), response.headers.get('cache-control'), textNodes(await response.text())])
      .toEqual(['anon', 'public, max-age=0, must-revalidate', ['CW storage', 'secret/path', 'scan 2026-10-12T1201', '99 TiB · 123 objects']])
    expect(mocks.match.mock.calls.map(call => call[0].url)).toEqual([
      'https://cw.example/og/map.png?policy=shape&r=8&s=2026-10-12T1201&tier=anon&h=0',
    ])
  })

  it('policy changes cannot reuse a warmed shape card', async () => {
    mocks.match.mockImplementation(async (key: Request) => new URL(key.url).searchParams.get('policy') === 'shape' ? new Response('old totals') : undefined)
    const response = await serveCard({ request: request('/og/map.png?policy=shape&r=2'), env: env({ OG_ANONYMOUS: 'generic' }) })
    expect([textNodes(await response.text()), mocks.match.mock.calls.map(call => call[0].url)]).toEqual([
      ['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', ''],
      ['https://cw.example/og/map.png?policy=generic&r=8'],
    ])
  })

  it('missing and unknown policy values fail closed; public deployments keep public cards', () => {
    expect([{}, { OG_ANONYMOUS: 'invalid' }, { OG_ANONYMOUS: 'shape' }, { OG_ANONYMOUS: 'full' }, { AUTH_MODE: 'public' }, { AUTH_MODE: 'public', OG_ANONYMOUS: 'invalid' }].map(anonymousPolicy))
      .toEqual(['generic', 'generic', 'shape', 'full', 'shape', 'generic'])
  })

  it('full policy: an anonymous fetch draws the full card (labelled), cached apart from the other policies', async () => {
    const response = await serveCard({ request: request('/og/map.png'), env: env({ OG_ANONYMOUS: 'full' }) })
    expect([response.headers.get('x-og-tier'), mocks.data.mock.calls.map(call => call[4]), textNodes(await response.text())])
      .toEqual(['anon', ['full'], ['CW storage', 'secret/path', 'scan 2026-10-12T1201', 'secret 99 B', '99 TiB · 123 objects']])
    expect(mocks.match.mock.calls.map(call => call[0].url)).toEqual([
      'https://cw.example/og/map.png?policy=full&r=8&s=2026-10-12T1201&tier=anon&h=0',
    ])
  })

  it('full policy: an anonymous page preview carries the view\'s scan and totals', async () => {
    await stampPage(env({ OG_CARDS: '1', OG_ANONYMOUS: 'full' }), new URL('https://cw.example/marin-us-east-02a/marin'), new Response('shell'))
    const meta = mocks.stamp.mock.calls[0][1]
    expect([meta.title, meta.desc, new URL(meta.image).searchParams.get('policy'), mocks.data.mock.calls.map(call => [call[3], call[4]])]).toEqual([
      'marin-us-east-02a/marin', 'scan 2026-10-12T1201 · 99 TiB · 123 objects', 'full',
      [[{ path: 'marin-us-east-02a/marin', d: '2026-10-12T1201' }, 'full']],
    ])
  })

  it('configured preview defaults match the deployment’s frontend color default', () => {
    expect([undefined, 'w', 'tree', 'invalid'].map(c => siteOf(env({ OG_DEFAULT_COLOR: c }))))
      .toEqual([
        { name: 'CW storage', scheme: 'gs://' },
        { name: 'CW storage', scheme: 'gs://', defaultColor: 'date' },
        { name: 'CW storage', scheme: 'gs://', defaultColor: 'tree' },
        { name: 'CW storage', scheme: 'gs://' },
      ])
  })

  it('HTML image URLs and render caches retain distinct selected color modes', async () => {
    const deployment = env({ OG_CARDS: '1', OG_ANONYMOUS: 'shape' })
    for (const c of ['t', 'u']) {
      await stampPage(deployment, new URL(`https://cw.example/?c=${c}`), new Response('shell'))
      const image = mocks.stamp.mock.calls.at(-1)![1].image
      await serveCard({ request: new Request(image), env: deployment })
    }
    expect(mocks.stamp.mock.calls.map(call => {
      const params = new URL(call[1].image).searchParams
      return [params.get('c'), params.get('r'), params.get('s')]
    })).toEqual([['t', '8', '2026-10-12T1201'], ['u', '8', '2026-10-12T1201']])
    expect(mocks.data.mock.calls.map(call => call[3])).toEqual([
      { c: 't', d: '2026-10-12T1201' }, { c: 'u', d: '2026-10-12T1201' },
    ])
    expect(mocks.match.mock.calls.map(call => new URL(call[0].url).searchParams.get('c'))).toEqual(['t', 'u'])
  })

  it('public anonymous previews do not require a session-signing secret', async () => {
    const response = await stampPage(env({ AUTH_MODE: 'public', OG_CARDS: '1', SESSION_SECRET: undefined }), new URL('https://public.example/?c=t'), new Response('shell'))
    expect([response.headers.get('cache-control'), mocks.stamp.mock.calls.map(call => {
      const image = new URL(call[1].image)
      return [image.origin, image.pathname, Object.fromEntries(image.searchParams)]
    })]).toEqual(['private, no-store', [['https://public.example', '/og/map.png', { c: 't', policy: 'shape', r: '8', s: '2026-10-12T1201' }]]])
  })
})

describe('genericTitle', () => {
  it('prefixes disky unless the label names it already', () => {
    expect([genericTitle('CW storage'), genericTitle(undefined), genericTitle('m3 disky metadata'), genericTitle('Disky demo'), genericTitle('diskyard')])
      .toEqual(['disky — CW storage', 'disky — storage', 'm3 disky metadata', 'Disky demo', 'disky — diskyard'])
  })
})

describe('a secondary store\'s pages unfurl as that store (`STORES_JSON` `path`)', () => {
  const STORES_JSON = JSON.stringify({
    meta: { path: '/meta', vars: { ROOT_LABEL: 'CW — meta', STORE_SCHEME: 'r2://' } },
    nas: { path: '/nas', vars: { ROOT_LABEL: 'nas', STORE_SCHEME: 'file:///', STORE_ROOT: 'mnt/user' } },
    staff: { path: '/staff', scope: 'admin', vars: { ROOT_LABEL: 'staff only', OG_ANONYMOUS: 'generic' } },
    ops: { path: '/ops', scope: 'admin', vars: { ROOT_LABEL: 'ops' } },
    hidden: { vars: { ROOT_LABEL: 'no page path' } },
  })
  const deployment = (extra: Partial<OgEnv> = {}) => env({ OG_CARDS: '1', STORES_JSON, STORE_ROOT: 'cw-root', OG_DEFAULT_COLOR: 'user', ...extra } as Partial<OgEnv>)
  const storeOf = (e: unknown) => (e as { STORE_KEY?: string }).STORE_KEY ?? 'primary'
  const full = () => mocks.tier.mockResolvedValue({ tier: 'full', day: expDay(Math.floor(Date.now() / 1000), 7), cred: { t: 'TokMetaAAA' } })

  it('page meta: its title, its data, and an image view carrying `store`; the image renders it from that store', async () => {
    full()
    mocks.live.mockResolvedValue(true)
    await stampPage(deployment(), new URL('https://cw.example/meta/snapshots?d=261012&og=TokMetaAAA'), new Response('shell'))
    const meta = mocks.stamp.mock.calls[0][1]
    const image = new URL(meta.image)
    const { sig, ...params } = Object.fromEntries(image.searchParams)
    expect([meta.title, meta.desc, image.pathname, params, sig.length]).toEqual([
      'snapshots', 'scan 2026-10-12T1201 · 99 TiB · 123 objects', '/og/map.png',
      { d: '261012', path: 'snapshots', policy: 'generic', r: '8', s: '2026-10-12T1201', store: 'meta', t: 'TokMetaAAA' }, 12,
    ])
    expect([
      mocks.tier.mock.calls.map(call => [storeOf(call[0]), call[1], call[2]]),
      mocks.scan.mock.calls.map(call => [storeOf(call[0]), call[1]]),
      mocks.data.mock.calls.map(call => [storeOf(call[0]), call[1], call[2], call[3], call[4]]),
    ]).toEqual([
      [['meta', 'map', { store: 'meta', path: 'snapshots', d: '261012' }]],
      [['meta', '261012']],
      [['meta', { name: 'CW — meta', scheme: 'r2://' }, 'snapshots', { store: 'meta', path: 'snapshots', d: '2026-10-12T1201' }, 'full']],
    ])
    mocks.data.mockClear()
    const response = await serveCard({ request: new Request(image), env: deployment() })
    expect([response.status, response.headers.get('x-og-tier')]).toEqual([200, 'full'])
    expect([
      mocks.live.mock.calls.map(call => [storeOf(call[0]), call[1], call[2], call[3]]),
      mocks.data.mock.calls.map(call => [storeOf(call[0]), call[1], call[2], call[3], call[4]]),
    ]).toEqual([
      [['meta', 'map', { store: 'meta', path: 'snapshots', d: '261012' }, { t: 'TokMetaAAA' }]],
      [['meta', { name: 'CW — meta', scheme: 'r2://' }, 'snapshots', { store: 'meta', path: 'snapshots', d: '2026-10-12T1201' }, 'full']],
    ])
  })

  it('its root is titled by its label; its own mount leads its paths; diffs too', async () => {
    const shape = deployment({ OG_ANONYMOUS: 'shape' })
    for (const p of ['/meta', '/nas', '/nas/media/tv', '/meta/snapshots?section=diff&d=261012-261010']) await stampPage(shape, new URL(`https://cw.example${p}`), new Response('shell'))
    expect(mocks.stamp.mock.calls.map(call => {
      const image = new URL(call[1].image)
      return [call[1].title, image.pathname, Object.fromEntries(image.searchParams)]
    })).toEqual([
      ['CW — meta', '/og/map.png', { policy: 'shape', r: '8', s: '2026-10-12T1201', store: 'meta' }],
      ['nas', '/og/map.png', { path: 'mnt/user', policy: 'shape', r: '8', s: '2026-10-12T1201', store: 'nas' }],
      ['media/tv', '/og/map.png', { path: 'mnt/user/media/tv', policy: 'shape', r: '8', s: '2026-10-12T1201', store: 'nas' }],
      ['secret/path', '/og/diff.png', { b: '2026-10-10T1201', d: '261012-261010', path: 'snapshots', policy: 'shape', r: '8', s: '2026-10-12T1201', store: 'meta' }],
    ])
    expect(mocks.diff.mock.calls.map(call => [storeOf(call[0]), call[1], call[2]])).toEqual([['meta', { name: 'CW — meta', scheme: 'r2://' }, 'snapshots · changes']])
    expect(mocks.pair.mock.calls.map(call => [storeOf(call[0]), call[1]])).toEqual([['meta', '261012-261010']])
  })

  it('the primary\'s pages are as before: no `store`, its label, scheme and mount', async () => {
    const shape = deployment({ OG_ANONYMOUS: 'shape' })
    for (const p of ['/', '/marin/x', '/metadata']) await stampPage(shape, new URL(`https://cw.example${p}`), new Response('shell'))
    expect(mocks.stamp.mock.calls.map(call => [call[1].title, Object.fromEntries(new URL(call[1].image).searchParams)])).toEqual([
      ['CW storage', { path: 'cw-root', policy: 'shape', r: '8', s: '2026-10-12T1201' }],
      ['marin/x', { path: 'cw-root/marin/x', policy: 'shape', r: '8', s: '2026-10-12T1201' }],
      ['metadata', { path: 'cw-root/metadata', policy: 'shape', r: '8', s: '2026-10-12T1201' }],
    ])
    expect(mocks.scan.mock.calls.map(call => storeOf(call[0]))).toEqual(['primary', 'primary', 'primary'])
    // without a store there, `/meta` is still no card
    const html = new Response('shell')
    expect(await stampPage(env({ OG_CARDS: '1', OG_ANONYMOUS: 'shape' }), new URL('https://cw.example/meta/snapshots'), html)).toBe(html)
  })

  it('a store\'s anonymous cards follow the deployment\'s `OG_ANONYMOUS` whatever its `scope` (that gates its pages), unless its vars set one', () => {
    const policy = (e: Partial<OgEnv>, page: string) => anonymousPolicy(pageOf(deployment(e), new URL(`https://cw.example${page}`))!.env)
    expect([
      policy({ OG_ANONYMOUS: 'full' }, '/ops/x'), policy({}, '/ops/x'), policy({ OG_ANONYMOUS: 'shape' }, '/meta/x'),
      policy({ OG_ANONYMOUS: 'full' }, '/staff/x'), policy({ OG_ANONYMOUS: 'full' }, '/x'),
    ]).toEqual(['full', 'generic', 'shape', 'generic', 'full'])
  })

  it('a store setting its own `OG_ANONYMOUS = generic` is generic to anonymous fetches, whatever the deployment\'s policy', async () => {
    const open = deployment({ OG_ANONYMOUS: 'full' })
    await stampPage(open, new URL('https://cw.example/staff/secret'), new Response('shell'))
    expect(mocks.stamp.mock.calls.map(call => call[1])).toEqual([{
      title: 'disky — staff only', desc: 'Private storage dashboard. Sign in to explore storage.',
      image: 'https://cw.example/og/map.png?policy=generic&r=8', imageType: 'image/png', page: 'https://cw.example/staff/secret',
    }])
    const response = await serveCard({ request: request('/og/map.png?store=staff&path=secret&policy=full&r=8'), env: open })
    expect([response.headers.get('x-og-tier'), textNodes(await response.text())])
      .toEqual(['anon', ['CW storage', 'Private storage dashboard', '', 'Sign in to explore storage', '']])
    expect([mocks.data.mock.calls, mocks.scan.mock.calls]).toEqual([[], []])
    const assets = vi.fn(async (_request: Request) => new Response('safe shell'))
    const shell = await anonymousPage({ ...open, ASSETS: { fetch: assets } }, new URL('https://cw.example/staff/secret'))
    expect([await shell!.text(), await anonymousPage(open, new URL('https://cw.example/meta/x'))]).toEqual(['safe shell', null])
  })

  it('a share token minted on its page is for its view (`store` included), never the primary\'s same path', async () => {
    const { db } = await sqliteD1()
    const dep = deployment()
    const m = (await mint(db, u => pageOf(dep, u)?.pv ?? null, new URL('https://cw.example/meta/snapshots?d=261012#diff'), 'ann@example.org', NOW, 30, 'TokMetaAAA'))!
    expect([
      m.url,
      await fullTier(db, 'diff', { store: 'meta', path: 'snapshots', d: '261012' }, 'TokMetaAAA', NOW),
      await fullTier(db, 'diff', { path: 'cw-root/snapshots', d: '261012' }, 'TokMetaAAA', NOW),
      await fullTier(db, 'diff', { path: 'snapshots', d: '261012' }, 'TokMetaAAA', NOW),
    ]).toEqual(['https://cw.example/meta/snapshots?d=261012&section=diff&og=TokMetaAAA#diff', { day: 304 }, null, null])
    expect([pageOf(dep, new URL('https://cw.example/staff/x'))?.env.STORE_SCOPE, pageOf(dep, new URL('https://cw.example/meta/x'))?.env.STORE_SCOPE]).toEqual(['admin', undefined])
  })

  it('an image naming no such store, or a store on a kind it has no page of, is not a card', async () => {
    const statuses: number[] = []
    for (const p of ['/og/map.png?store=nope', '/og/map.png?store=constructor', '/og/staged.png?store=meta', '/og/users.png?store=meta']) {
      statuses.push((await serveCard({ request: request(p), env: deployment({ OG_ANONYMOUS: 'shape' }) })).status)
    }
    expect([statuses, mocks.data.mock.calls, mocks.scan.mock.calls]).toEqual([[404, 404, 404, 404], [], []])
  })
})
