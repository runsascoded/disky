import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'

import { A, unionDeployment } from '../_lib/testUnion'
import { type Hit, injectedStores, type StaticFilterStore } from '../_lib/staticFilter'
import { REJECT_MESSAGES } from '../_lib/indexedOnly'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as filterScans } from './filter-scans'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// A store reads a static name index only when its OWN config declares one (`STORES_JSON` `vars.STATIC_GEN`, in the
// deployment's `INDEX_R2`): the deployment's top-level static config is the primary's. A secondary store without
// one is `scan-not-indexed` for a literal — never the primary's index (whose scan ids may coincide with its own),
// never a walk. The deployment: `_lib/testUnion.ts` (primary roots `bk`, `zz`; `meta`'s root `mb`), each store's
// static index a stub (`injectedStores`, per store).

/** A live first hit at `path` (every scan). */
const hit = (path: string, size: number): Hit => ({ path, depth: path.split('/').length, usr: '', vf: 0, vt: Number.MAX_SAFE_INTEGER, size: BigInt(size), n: 1n })

/** A static store over `table` (literal → its match roots), holding `scans`. */
function stub(gen: string, scans: string[], table: Record<string, Hit[]>): StaticFilterStore {
  return {
    gen,
    scans: async () => scans,
    source: { async hits(key, under) { return { hits: (table[key] ?? []).filter(h => under === '' || h.path.startsWith(`${under}/`)), io: {} } } },
  }
}
/** The primary's index: `ckpt` matches `bk/ckpt`. */
const PRIMARY_INDEX = stub('gen-primary', [A], { ckpt: [hit('bk/ckpt', 6000)] })
/** `meta`'s own index: `index` matches `mb/index`; `ckpt` nothing. */
const META_INDEX = stub('gen-meta', [A], { index: [hit('mb/index', 2097152)] })

const META = { vars: { ROOT_LABEL: 'our storage', SNAPSHOTS_SUBDIR: 'meta', STORE_PREFIXES: 'meta-l2/,snapshots/meta/', STORE_BUCKET: 'meta-data' }, secrets: { STORE_ACCESS_KEY_ID: 'STORE_META_ID', STORE_SECRET_ACCESS_KEY: 'STORE_META_ID' } }

type Body = { q?: string; matches?: string[]; matched?: { path: string; b: number; o: number }[]; matchCount?: { n: number; b: number; o: number }; tree: { b: number } }

describe('D1', () => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    deployment = await unionDeployment()
  })

  /** The deployment with the static filter on (the primary's index), no union view; `meta` as configured. */
  const envOf = (meta: Record<string, unknown> = META, indexes: Record<string, StaticFilterStore> = {}): Env => {
    const env = { ...deployment({}), FILTER_STATIC: '1', STATIC_GEN: 'gen-primary', INDEX_R2: {} as R2Bucket } as Env
    env.STORES_JSON = JSON.stringify({ ...JSON.parse(env.STORES_JSON!), meta })
    injectedStores.set(env, PRIMARY_INDEX)
    for (const [store, s] of Object.entries(indexes)) injectedStores.set(env, s, store)
    return env
  }
  const get = async (env: Env, qs: string): Promise<[number, unknown]> => {
    const r = await subtree({ request: new Request(`http://localhost/api/subtree?w=1280&h=768&date=${A}&${qs}`), env })
    const text = await r.text()
    return [r.status, r.headers.get('content-type')?.startsWith('application/json') ? JSON.parse(text) : text]
  }
  const matchesOf = ([status, b]: [number, unknown]) => [status, (b as Body).matched, (b as Body).matchCount]

  it('the primary answers from its own index, as before', async () => {
    expect(matchesOf(await get(envOf(), 'path=&q=ckpt'))).toEqual([200, [{ path: 'bk/ckpt', b: 6000, o: 1 }], { n: 1, b: 6000, o: 1 }])
  })

  it('a secondary store without its own index is `scan-not-indexed` — never the primary’s matches on a coinciding scan id', async () => {
    const refusal = [400, { error: REJECT_MESSAGES['scan-not-indexed'], code: 'scan-not-indexed' }]
    const env = envOf()
    expect([await get(env, 'store=meta&path=&q=ckpt'), await get(env, 'store=meta&path=mb&q=index'), await get(env, 'store=meta&path=&q=index%20-ckpt')])
      .toEqual([refusal, refusal, refusal])
  })

  it('a secondary store with its own `STATIC_GEN` answers from its own index only', async () => {
    const env = envOf({ ...META, vars: { ...META.vars, STATIC_GEN: 'gen-meta' } }, { meta: META_INDEX })
    expect([matchesOf(await get(env, 'store=meta&path=&q=index')), matchesOf(await get(env, 'store=meta&path=&q=ckpt'))]).toEqual([
      [200, [{ path: 'mb/index', b: 2097152, o: 1 }], { n: 1, b: 2097152, o: 1 }],
      [200, [], undefined],
    ])
  })

  it('`/api/filter-scans` answers for the store asked: its own index’s scans, none without one', async () => {
    const scans = async (env: Env, qs: string) => (await filterScans({ request: new Request(`http://localhost/api/filter-scans?${qs}`), env } as never)).json()
    const bare = envOf()
    const own = envOf({ ...META, vars: { ...META.vars, STATIC_GEN: 'gen-meta' } }, { meta: stub('gen-meta', ['2026-10-02T0001'], {}) })
    expect([await scans(bare, ''), await scans(bare, 'store=meta'), await scans(own, 'store=meta')])
      .toEqual([{ scans: [A] }, { scans: [] }, { scans: ['2026-10-02T0001'] }])
  })
})
