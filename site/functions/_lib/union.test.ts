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
      { all: { key: 'all', members: ['primary', 'meta'], main: true }, alt: { key: 'alt', members: ['meta', 'primary'], main: false } },
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
      'STORES_JSON.all: unknown field(s) vars (a union view takes union, main)',
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

describe('unionFor', () => {
  const req = (qs: string) => new Request(`https://x.test/api/subtree?${qs}`)
  const main = env({ meta: META, all: { union: ['primary', 'meta'], main: true } })
  const named = env({ meta: META, all: { union: ['primary', 'meta'] } })
  const view = { key: 'all', members: ['primary', 'meta'], main: true }
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
  const view = { key: 'all', members: ['primary', 'meta'], main: true }
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
  it('empty members: a childless root; a root two members draw is refused', () => {
    expect(unionRoot(view, 'Fleet', [{ store: 'primary', date: 'A', tree: leaf('p', 0, 0) }, { store: 'meta', date: 'A', tree: leaf('m', 0, 0) }]))
      .toEqual({ n: 'Fleet', k: 'dir', b: 0, o: 0 })
    const f = () => unionRoot(view, 'Fleet', [{ store: 'primary', date: 'A', tree: { ...leaf('p', 5), c: [leaf('x', 5)] } }, { store: 'meta', date: 'A', tree: { ...leaf('m', 5), c: [leaf('x', 5)] } }])
    expect(f).toThrow(new UnionOverlap("union view 'all': root 'x' is in stores primary, meta"))
  })
})
