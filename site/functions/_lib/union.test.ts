import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { secondaryStores, unionViews } from './stores'
import { memberRequest, unionFor, unionRoot, UnionOverlap } from './union'
import type { ViewNode } from './view'

// Union views' config (`STORES_JSON` entries with `union`), request routing, and the root merge.

const META = { vars: { ROOT_LABEL: 'meta' } }
const env = (json: unknown): Env => ({ STORES_JSON: JSON.stringify(json) }) as Env
const err = (json: unknown) => {
  try { unionViews(env(json)) } catch (e) { return (e as Error).message }
  return null
}

describe('STORES_JSON union views', () => {
  it('a `union` entry is a view, not a store; `main` defaults off', () => {
    const e = env({ meta: META, all: { union: ['primary', 'meta'], main: true }, alt: { union: ['meta', 'primary'] } })
    expect([secondaryStores(e), unionViews(e)]).toEqual([
      { meta: META },
      { all: { key: 'all', members: ['primary', 'meta'], main: true, sources: {} }, alt: { key: 'alt', members: ['meta', 'primary'], main: false, sources: {} } },
    ])
    expect(unionViews({} as Env)).toEqual({})
  })
  it('refuses a malformed view', () => {
    expect([
      err({ meta: META, all: { union: ['primary', 'meta'], vars: {} } }),
      err({ meta: META, all: { union: ['primary'] } }),
      err({ meta: META, all: { union: 'primary,meta' } }),
      err({ meta: META, all: { union: ['primary', 'primary'] } }),
      err({ meta: META, all: { union: ['primary', 'nope'] } }),
      err({ meta: META, all: { union: ['primary', 'meta'] }, both: { union: ['primary', 'all'] } }),
      err({ meta: META, all: { union: ['primary', 'meta'], main: 'yes' } }),
      err({ meta: META, a: { union: ['primary', 'meta'], main: true }, b: { union: ['meta', 'primary'], main: true } }),
    ]).toEqual([
      'STORES_JSON.all: unknown field(s) vars (a union view takes union, main, sources)',
      'STORES_JSON.all.union: want ≥ 2 store keys',
      'STORES_JSON.all.union: want ≥ 2 store keys',
      'STORES_JSON.all.union: a store is named twice',
      "STORES_JSON.all.union: 'nope' is not a store (want 'primary' or a store key)",
      "STORES_JSON.both.union: 'all' is not a store (want 'primary' or a store key)",
      'STORES_JSON.all.main: want a boolean',
      'STORES_JSON: more than one main union view (a, b)',
    ])
  })
})

describe('STORES_JSON union view `sources` (the picker’s and the map’s marks)', () => {
  it('each named member’s label and icon (a URL or a site path); members it doesn’t name are unmarked', () => {
    const e = env({ meta: META, all: { union: ['primary', 'meta'], main: true, sources: {
      primary: { label: 'Buckets', icon: '/icons/buckets.svg' }, meta: { icon: 'https://example.com/meta.png' },
    } } })
    expect(unionViews(e)).toEqual({ all: { key: 'all', members: ['primary', 'meta'], main: true, sources: {
      primary: { label: 'Buckets', icon: '/icons/buckets.svg' }, meta: { icon: 'https://example.com/meta.png' },
    } } })
  })
  it('refuses a non-member, an unknown field, an empty label, an icon that is neither a URL nor a site path', () => {
    const view = (sources: unknown) => ({ meta: META, all: { union: ['primary', 'meta'], sources } })
    expect([
      err(view([])),
      err(view({ nope: { label: 'x' } })),
      err(view({ meta: 'x' })),
      err(view({ meta: { label: 'x', color: 'red' } })),
      err(view({ meta: { label: ' ' } })),
      err(view({ meta: { icon: 'icons/meta.svg' } })),
      err(view({ meta: { icon: 'javascript:alert(1)' } })),
      err(view({ meta: { icon: '//evil.example/x.png' } })),
    ]).toEqual([
      'STORES_JSON.all.sources: want an object of member → {label, icon}',
      "STORES_JSON.all.sources: 'nope' is not a member",
      'STORES_JSON.all.sources.meta: want an object',
      'STORES_JSON.all.sources.meta: unknown field(s) color (want label, icon)',
      'STORES_JSON.all.sources.meta.label: want a nonempty string',
      'STORES_JSON.all.sources.meta.icon: want an http(s) URL or a site path (/…)',
      'STORES_JSON.all.sources.meta.icon: want an http(s) URL or a site path (/…)',
      'STORES_JSON.all.sources.meta.icon: want an http(s) URL or a site path (/…)',
    ])
  })
})

describe('unionFor', () => {
  const req = (qs: string) => new Request(`https://x.test/api/subtree?${qs}`)
  const main = env({ meta: META, all: { union: ['primary', 'meta'], main: true } })
  const named = env({ meta: META, all: { union: ['primary', 'meta'] } })
  const view = { key: 'all', members: ['primary', 'meta'], main: true, sources: {} }
  it('no `store=` is the main view; `store=<view>` that view; a store key (`primary` included) that store alone', () => {
    expect([unionFor(req('date=x'), main), unionFor(req('store='), main), unionFor(req('store=all'), main), unionFor(req('store=primary'), main), unionFor(req('store=meta'), main)])
      .toEqual([view, view, view, null, null])
    expect([unionFor(req('date=x'), named), unionFor(req('store=all'), named)]).toEqual([null, { ...view, main: false }])
    expect(unionFor(req(''), {} as Env)).toBe(null)
  })
  it('a malformed config is a 500', async () => {
    const r = unionFor(req(''), env({ all: { union: ['primary', 'nope'] } })) as Response
    expect([r.status, await r.text()]).toEqual([500, '{"error":"STORES_JSON.all.union: \'nope\' is not a store (want \'primary\' or a store key)"}\n'])
  })
})

describe('memberRequest', () => {
  it('names the member (none for the primary), replaces and drops params, keeps headers', () => {
    const r = new Request('https://x.test/api/subtree?d=2610&path=a&store=all', { headers: { cookie: 's=1' } })
    const p = memberRequest(r, 'primary', { date: '2026-10-01' }, ['d'])
    const m = memberRequest(r, 'meta', { date: '2026-09-30' }, ['d'])
    expect([p.url, m.url, m.headers.get('cookie')]).toEqual([
      'https://x.test/api/subtree?path=a&date=2026-10-01',
      'https://x.test/api/subtree?path=a&store=meta&date=2026-09-30',
      's=1',
    ])
  })
})

describe('unionRoot', () => {
  const view = { key: 'all', members: ['primary', 'meta'], main: true, sources: {} }
  const leaf = (n: string, b: number, o = 1): ViewNode => ({ n, k: 'dir', b, o })
  it('tags each member’s roots, sorts them, sums the `(other)`s (last) and the totals', () => {
    const primary = { ...leaf('primary root', 1000, 10), c: [leaf('a', 600, 5), leaf('b', 300, 4), { ...leaf('(other)', 100, 1), f: 3 }] }
    const meta = { ...leaf('meta root', 450, 6), c: [leaf('m', 400, 5), { ...leaf('(other)', 50, 1), f: 2 }] }
    expect(unionRoot(view, 'Fleet', [{ store: 'primary', date: 'A', tree: primary }, { store: 'meta', date: 'B', tree: meta }])).toEqual({
      n: 'Fleet', k: 'dir', b: 1450, o: 16,
      c: [
        { ...leaf('a', 600, 5), src: 'primary', at: 'A' },
        { ...leaf('m', 400, 5), src: 'meta', at: 'B' },
        { ...leaf('b', 300, 4), src: 'primary', at: 'A' },
        { n: '(other)', k: 'dir', b: 150, o: 2, f: 5 },
      ],
    })
  })
  it('with each member’s root rows, every root the threshold folded is listed too, out of its `(other)`', () => {
    // r2's shape: the primary's three buckets drawn, meta's two (1.9 GB, 1.5 GB) and a 23 MB one under the
    // union's ~2.2 GB threshold — all three folded into meta's `(other)`.
    const primary = { ...leaf('primary root', 1_000_000_000_000, 30), c: [leaf('ctbk', 900_000_000_000, 10), leaf('crashes', 90_000_000_000, 10), leaf('jc-taxes', 10_000_000_000, 10)] }
    const meta = { ...leaf('meta root', 3_423_000_000, 7), c: [{ ...leaf('(other)', 3_423_000_000, 7), f: 3 }] }
    const metaRows = [
      { path: 'oa-r2-usage-index', b: 1_900_000_000, o: 4, m: 86400 * 20000 + 5, k: 'dir' as const },
      { path: 'disk-tree-demo', b: 1_500_000_000, o: 2, m: null, k: 'dir' as const },
      { path: 'tiny', b: 23_000_000, o: 1 },
    ]
    const primaryRows = [{ path: 'ctbk', b: 900_000_000_000, o: 10 }, { path: 'crashes', b: 90_000_000_000, o: 10 }, { path: 'jc-taxes', b: 10_000_000_000, o: 10 }]
    expect(unionRoot(view, 'Fleet', [
      { store: 'primary', date: 'A', tree: primary, roots: primaryRows },
      { store: 'meta', date: 'A', tree: meta, roots: metaRows },
    ])).toEqual({
      n: 'Fleet', k: 'dir', b: 1003_423_000_000, o: 37,
      c: [
        { ...leaf('ctbk', 900_000_000_000, 10), src: 'primary', at: 'A' },
        { ...leaf('crashes', 90_000_000_000, 10), src: 'primary', at: 'A' },
        { ...leaf('jc-taxes', 10_000_000_000, 10), src: 'primary', at: 'A' },
        { n: 'oa-r2-usage-index', k: 'dir', b: 1_900_000_000, o: 4, d: 20000, src: 'meta', at: 'A' },
        { n: 'disk-tree-demo', k: 'dir', b: 1_500_000_000, o: 2, src: 'meta', at: 'A' },
        { n: 'tiny', k: 'dir', b: 23_000_000, o: 1, src: 'meta', at: 'A' },
      ],
    })
  })
  it('a lifted root another member draws is still an overlap', () => {
    const f = () => unionRoot(view, 'Fleet', [
      { store: 'primary', date: 'A', tree: { ...leaf('p', 5), c: [leaf('x', 5)] } },
      { store: 'meta', date: 'A', tree: { ...leaf('m', 1), c: [{ ...leaf('(other)', 1), f: 1 }] }, roots: [{ path: 'x', b: 1, o: 1 }] },
    ])
    expect(f).toThrow(new UnionOverlap("union view 'all': root 'x' is in stores primary, meta"))
  })
  it('empty members: a childless root; a root two members draw is refused', () => {
    expect(unionRoot(view, 'Fleet', [{ store: 'primary', date: 'A', tree: leaf('p', 0, 0) }, { store: 'meta', date: 'A', tree: leaf('m', 0, 0) }]))
      .toEqual({ n: 'Fleet', k: 'dir', b: 0, o: 0 })
    const f = () => unionRoot(view, 'Fleet', [{ store: 'primary', date: 'A', tree: { ...leaf('p', 5), c: [leaf('x', 5)] } }, { store: 'meta', date: 'A', tree: { ...leaf('m', 5), c: [leaf('x', 5)] } }])
    expect(f).toThrow(new UnionOverlap("union view 'all': root 'x' is in stores primary, meta"))
  })
})
