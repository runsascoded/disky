import { beforeAll, describe, expect, it, vi } from 'vitest'
import { type Env, gateFor } from '../_lib/auth'
import { hideScope } from '../_lib/restrict'
import { parseClasses } from '../_lib/scope'
import { type Hit, injectedStores, type StaticFilterStore } from '../_lib/staticFilter'
import { A, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as diff } from './diff'
import { onRequestGet as series } from './series'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// A name filter (`q=`) over a path-restricted link: its answers are sums over every match, and no route can take a
// hidden path's hits back out of a sum (a filtered `tree`'s bytes, a filtered series' points, a filtered diff's rows).
// So it is refused: otherwise the link could probe a hidden directory's contents by name. The deployment:
// `_lib/testUnion.ts`'s primary (roots `bk`, `zz`), with the static name index a stub whose `ckpt` is `bk/ckpt`.

const SEARCH_REFUSED = [403, '{"error":"search isn’t available on this link","code":"restricted-search"}\n']
const SCOPE_REFUSED = [403, '{"error":"this owner, class or user view isn’t available here on this link","code":"restricted-scope"}\n']
const hit = (path: string, size: number): Hit => ({ path, depth: path.split('/').length, usr: '', vf: 0, vt: Number.MAX_SAFE_INTEGER, size: BigInt(size), n: 1n })
const INDEX: StaticFilterStore = {
  gen: 'gen-primary',
  scans: async () => [Z, A],
  source: { async hits(key, under) { return { hits: (key === 'ckpt' ? [hit('bk/ckpt', 6000)] : []).filter(h => under === '' || h.path.startsWith(`${under}/`)), io: {} } } },
}

describe('D1', () => {
  let env: Env
  let restricted: string
  let open: string

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => undefined } }
    const deployment = await unionDeployment()
    env = { ...deployment({}), BASE_SCOPE: 'gcs', SESSION_SECRET: 's'.repeat(32), FILTER_STATIC: '1', STATIC_GEN: 'gen-primary', INDEX_R2: {} as R2Bucket } as Env
    injectedStores.set(env, INDEX)
    const mint = async (scopes: string[]) => (await gateFor(env)!.mint({ email: null, scopes, name: 'link', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
    restricted = await mint(['gcs:read', hideScope('gcs', 'bk')])
    open = await mint(['gcs:read'])
  })

  const get = async (route: typeof subtree, url: string, token: string): Promise<[number, string]> => {
    const r = await route({ request: new Request(`https://site.example.test${url}`, { headers: { authorization: `Bearer ${token}` } }), env })
    return [r.status, await r.text()]
  }
  const SUBTREE = `/api/subtree?date=${A}&w=1280&h=768&q=ckpt`
  const DIFF = `/api/diff?from=${Z}&to=${A}&w=1280&h=768&q=ckpt`
  const SERIES = '/api/series?q=ckpt'

  it('a restricted link is refused a name filter, on the map, the diff and the chart, whatever it matches', async () => {
    expect([
      await get(subtree, SUBTREE, restricted),
      await get(diff, DIFF, restricted),
      await get(series, SERIES, restricted),
      await get(subtree, `/api/subtree?date=${A}&w=1280&h=768&q=nomatch`, restricted),
    ]).toEqual([SEARCH_REFUSED, SEARCH_REFUSED, SEARCH_REFUSED, SEARCH_REFUSED])
  })

  it('a union root\'s name filter: refused when its members refuse, whichever member\'s index holds the match', async () => {
    const deployment = await unionDeployment()
    const union = { ...deployment(), BASE_SCOPE: 'gcs', SESSION_SECRET: 's'.repeat(32), FILTER_STATIC: '1', STATIC_GEN: 'gen-primary', INDEX_R2: {} as R2Bucket } as Env
    injectedStores.set(union, INDEX)
    const { token } = await gateFor(union)!.mint({ email: null, scopes: ['gcs:read', hideScope('gcs', 'bk'), hideScope('meta', 'mb')], name: 'link', createdBy: 'test', expiresAt: null, maxRedeems: null })
    const r = await subtree({ request: new Request(`https://site.example.test/api/subtree?at=2026-10-01T00:01:00.000Z&w=1280&h=768&q=ckpt`, { headers: { authorization: `Bearer ${token}` } }), env: union })
    expect([r.status, await r.text()]).toEqual(SEARCH_REFUSED)
  })

  it('the same link without its restriction gets the filter\'s answer, and the restricted one its plain views', async () => {
    const statuses = await Promise.all([
      get(subtree, SUBTREE, open),
      get(diff, DIFF, open),
      get(series, SERIES, open),
      get(subtree, `/api/subtree?date=${A}&w=1280&h=768`, restricted),
      get(subtree, `/api/subtree?date=${A}&w=1280&h=768&q=`, restricted),
      get(series, '/api/series', restricted),
    ])
    expect(statuses.map(([s]) => s)).toEqual([200, 200, 200, 200, 200, 200])
  })

  // An owner, class or lens scope narrows a sum to what's below the path, and a sum can't be filtered on the way out:
  // a hidden dir's own cell under `o=`/`cl=`/`lens=` is the split of what it hides (`scopeReachesHidden`). So is an
  // ancestor's: its scoped total, less its visible children's, is the hidden dir's.
  describe('an owner, class or lens scope', () => {
    // A class this deployment has: another's letter is no scope at all, which proves nothing about the gate.
    const CLASS = [...'abcdefghijklmnopqrstuvwxyz'].find(l => parseClasses(l))
    const SCOPES = ['o=*', 'o=', 'o=!someone', `cl=${CLASS}`, 'lens=user:someone', 'lens=user:someone&by=other']
    const NOT_FOUND = [404, 'path not found']
    const routes: [string, typeof subtree, (q: string) => string][] = [
      ['subtree', subtree, q => `/api/subtree?date=${A}&w=1280&h=768&${q}`],
      ['diff', diff, q => `/api/diff?from=${Z}&to=${A}&w=1280&h=768&${q}`],
      ['series', series, q => `/api/series?${q}`],
    ]
    let deep: string
    beforeAll(async () => {
      deep = (await gateFor(env)!.mint({ email: null, scopes: ['gcs:read', hideScope('gcs', 'bk/deep')], name: 'link', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
    })

    describe.each(routes)('`/api/%s`', (_, route, url) => {
      it.each(SCOPES)('`%s` at the hidden dir, or at an ancestor of it: refused', async scope => {
        expect([
          await get(route, url(`path=bk&${scope}`), restricted),
          await get(route, url(`path=bk&${scope}`), deep),
          await get(route, url(scope), deep),
          await get(route, url(scope), restricted),
        ]).toEqual([SCOPE_REFUSED, SCOPE_REFUSED, SCOPE_REFUSED, SCOPE_REFUSED])
      })
      it.each(SCOPES)('`%s` under the hidden dir: absent, as an unscoped read is', async scope => {
        expect(await get(route, url(`path=bk/deep&${scope}`), restricted)).toEqual(NOT_FOUND)
      })
      it.each(SCOPES)('`%s` beside the hidden dir: answered as the unrestricted link answers', async scope => {
        const [[status, body], [openStatus, openBody]] = [await get(route, url(`path=zz&${scope}`), restricted), await get(route, url(`path=zz&${scope}`), open)]
        expect([status, body]).toEqual([openStatus, openBody])
      })
      it('unscoped, the hidden dir and its ancestors keep their totals', async () => {
        const statuses = await Promise.all([url('path=bk'), url('path=bk&o=x'), url('')].map(u => get(route, u, restricted)))
        expect(statuses.map(([status]) => status)).toEqual([200, 200, 200])
      })
    })
  })
})
