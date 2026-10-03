import { describe, expect, it } from 'vitest'
import { resolveStores, storeFetch, storeForPath, storeQuery, storeUrl, withPageConfig, type Store } from './stores'
import { TEST_REGISTRY } from './testStores'

// The build-time store resolution (specs/multi-store.md phase 2): `VITE_STORE`
// picks the primary, `VITE_STORES_EXTRA` mounts secondaries under their own
// paths, and only a secondary store's requests carry `store=<key>` — the
// primary's URLs are exactly what they were before stores existed.

const keys = (ss: Store[]) => ss.map(s => s.key)
const thrown = (f: () => unknown): string | null => {
  try { f() } catch (e) { return (e as Error).message }
  return null
}

describe('resolveStores', () => {
  it('nothing configured: the registry’s first store alone', () => {
    expect(keys(resolveStores(undefined, undefined, TEST_REGISTRY))).toEqual(['cw'])
  })
  it('VITE_STORE picks the primary; no extras', () => {
    expect(keys(resolveStores('r2', '', TEST_REGISTRY))).toEqual(['r2'])
  })
  it('VITE_STORES_EXTRA mounts secondaries after the primary, each at its own path and base', () => {
    const [r2, meta] = resolveStores('r2', 'meta', TEST_REGISTRY)
    expect([r2.key, r2.path, r2.base]).toEqual(['r2', '/', '/data/r2'])
    expect({ key: meta.key, path: meta.path, base: meta.base, label: meta.label, title: meta.title, staging: meta.staging, owners: meta.owners, prices: meta.prices }).toEqual({
      key: 'meta',
      path: '/meta',
      base: '/data/meta',
      label: 'Meta',
      title: 'Our storage — scan & index data',
      staging: false,
      owners: false,
      prices: false,
    })
  })
  it('extras are trimmed and de-duplicated, empties dropped', () => {
    expect(keys(resolveStores('cw', ' meta, ,meta,', TEST_REGISTRY))).toEqual(['cw', 'meta'])
  })
  it('refuses an unknown key, as primary or extra', () => {
    expect(thrown(() => resolveStores('nope', undefined, TEST_REGISTRY))).toBe("stores: no registry store 'nope' (have cw, gcs, laptop, meta, r2)")
    expect(thrown(() => resolveStores('cw', 'nope', TEST_REGISTRY))).toBe("stores: no registry store 'nope' (have cw, gcs, laptop, meta, r2)")
  })
  it('refuses the primary among the extras', () => {
    expect(thrown(() => resolveStores('cw', 'cw', TEST_REGISTRY))).toBe("stores: 'cw' is the primary store (VITE_STORE); it can't also be in VITE_STORES_EXTRA")
  })
  it('refuses a secondary that has no path of its own', () => {
    expect(thrown(() => resolveStores('cw', 'gcs', TEST_REGISTRY))).toBe("stores: secondary store 'gcs' has no path of its own (path '/' is the primary's)")
  })
})

describe('store requests', () => {
  const [r2, meta] = resolveStores('r2', 'meta', TEST_REGISTRY)
  it('the primary carries no store param; a secondary carries its key', () => {
    expect([storeQuery(r2, r2), storeQuery(meta, r2)]).toEqual(['', 'store=meta'])
  })
  it('storeUrl leaves the primary’s URLs untouched', () => {
    const urls = ['/data/r2/scans.json', '/data/r2/2026-09-29/meta.json', '/api/subtree?cv=2&date=2026-09-29&path=', '/v1/files/list?prefix=snapshots/']
    expect(urls.map(u => storeUrl(u, r2, r2))).toEqual(urls)
  })
  it('storeUrl appends store=<key> to a secondary’s URLs, with ? or & as needed, before any fragment', () => {
    expect([
      storeUrl('/data/meta/scans.json', meta, r2),
      storeUrl('/data/meta/2026-09-29/meta.json', meta, r2),
      storeUrl('/api/subtree?cv=2&date=2026-09-29&path=', meta, r2),
      storeUrl('/api/store', meta, r2),
      storeUrl('/v1/files/list?prefix=snapshots/#x', meta, r2),
    ]).toEqual([
      '/data/meta/scans.json?store=meta',
      '/data/meta/2026-09-29/meta.json?store=meta',
      '/api/subtree?cv=2&date=2026-09-29&path=&store=meta',
      '/api/store?store=meta',
      '/v1/files/list?prefix=snapshots/&store=meta#x',
    ])
  })
  it('storeFetch is the given fetch itself for the primary', () => {
    const impl = (async () => new Response('')) as unknown as typeof fetch
    expect(storeFetch(r2, r2, impl)).toBe(impl)
  })
  it('storeFetch rewrites a secondary’s string, URL and Request inputs, passing init through', async () => {
    const calls: [string, RequestInit | undefined][] = []
    const impl = (async (input: string | URL | Request, init?: RequestInit) => {
      calls.push([typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, init])
      return new Response('')
    }) as typeof fetch
    const f = storeFetch(meta, r2, impl)
    await f('/api/series?path=', { credentials: 'include' })
    await f(new URL('https://h.test/data/meta/scans.json'))
    await f(new Request('https://h.test/v1/files/list?prefix=snapshots/'))
    expect(calls).toEqual([
      ['/api/series?path=&store=meta', { credentials: 'include' }],
      ['https://h.test/data/meta/scans.json?store=meta', undefined],
      ['https://h.test/v1/files/list?prefix=snapshots/&store=meta', undefined],
    ])
  })
})

describe('storeForPath', () => {
  const stores = resolveStores('r2', 'meta', TEST_REGISTRY)
  it('a secondary’s path and everything under it is that store; anything else is the primary', () => {
    expect(['/', '/ctbk/x', '/files', '/meta', '/meta/', '/meta/my-data/listing', '/meta/files', '/metadata'].map(p => storeForPath(p, stores).key))
      .toEqual(['r2', 'r2', 'r2', 'meta', 'meta', 'meta', 'meta', 'r2'])
  })
  it('a single-store build is always the primary', () => {
    expect(['/', '/meta', '/meta/x'].map(p => storeForPath(p, resolveStores('cw', '', TEST_REGISTRY)).key)).toEqual(['cw', 'cw', 'cw'])
  })
})

describe('withPageConfig', () => {
  const laptop = resolveStores('laptop', '', TEST_REGISTRY)[0]
  it('the served page’s home and title replace the build’s', () => {
    const s = withPageConfig(laptop, { home: '/Users/someone/', title: 'disky — Some Mac' })
    expect([s.key, s.home, s.title, s.base]).toEqual(['laptop', ['Users', 'someone'], 'disky — Some Mac', laptop.base])
  })
  it('no page config (any hosted deploy): the store as built', () => {
    expect(withPageConfig(laptop, undefined)).toBe(laptop)
    expect(withPageConfig(laptop, {})).toEqual(laptop)
  })
})
