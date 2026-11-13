import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'

import { A, B, C, unionDeployment } from '../_lib/testUnion'
import { type Hit, injectedStores, type StaticFilterStore } from '../_lib/staticFilter'
import { REJECT_MESSAGES } from '../_lib/indexedOnly'
import { filterThreshold } from '../_lib/filter'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as filterCover } from './filter-cover'
import { onRequestGet as filterCaps } from './filter-caps'
import { onRequestGet as filterScans } from './filter-scans'
import { onRequestGet as diff } from './diff'
import { onRequestGet as series } from './series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// A union view's name filter (specs/federated-scans.md, case 1 slice 5): the root's `q=` is each member's own `q=`
// — its roots, its resolved scan, its own static name index — merged as the plain root merges; a member without an
// index of its own refuses for its mount (`scan-not-indexed`), named, never a walk or another store's index. The
// deployment: `_lib/testUnion.ts` (primary roots `bk`, `zz`; `meta`'s root `mb`), the static filter on, each
// store's index a stub (`injectedStores`, per store).

const hit = (path: string, size: number): Hit => ({ path, depth: path.split('/').length, usr: '', vf: 0, vt: Number.MAX_SAFE_INTEGER, size: BigInt(size), n: 1n })
function stub(gen: string, scans: string[], table: Record<string, Hit[]>): StaticFilterStore {
  return {
    gen,
    scans: async () => scans,
    source: { async hits(key, under) { return { hits: (table[key] ?? []).filter(h => under === '' || h.path.startsWith(`${under}/`)), io: {} } } },
  }
}
/** `ckpt`: the primary's `bk/ckpt` alone; `both`: a root in each store. */
const PRIMARY_INDEX = stub('gen-primary', [A], { ckpt: [hit('bk/ckpt', 6000)], both: [hit('bk/ckpt', 6000)] })
const META_INDEX = stub('gen-meta', [A, B], { both: [hit('mb/scans', 4194304)] })

const META = { vars: { ROOT_LABEL: 'our storage', SNAPSHOTS_SUBDIR: 'meta', STORE_PREFIXES: 'meta-l2/,snapshots/meta/', STORE_BUCKET: 'meta-data' }, secrets: { STORE_ACCESS_KEY_ID: 'STORE_META_ID', STORE_SECRET_ACCESS_KEY: 'STORE_META_ID' } }
const META_OWN = { ...META, vars: { ...META.vars, STATIC_GEN: 'gen-meta' } }
const NOT_INDEXED = { code: 'scan-not-indexed', error: REJECT_MESSAGES['scan-not-indexed'] }

type Node = { name: string; kind: string; bytes: number; objects: number; children?: Node[]; store?: string; scan?: string; matched?: true }
type Body = Record<string, unknown> & { tree: Node; nodes: number; tier: string; index: string; matched?: unknown; matches?: unknown; matchCount?: unknown }

describe('D1', () => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { puts.push(k.url) } } }
    deployment = await unionDeployment()
  })
  const puts: string[] = []
  /** The subtree reads cached since `puts` was cleared: each one's store and union threshold (`&T=`, absent: its own). */
  const reads = () => puts.filter(k => k.startsWith('https://subtree.cache/')).map(k => {
    const u = decodeURIComponent(k)
    return [/\/@([^/]+)\//.exec(u)?.[1] ?? 'primary', /&T=([^&]+)/.exec(u)?.[1] ?? null]
  }).sort()

  /** The deployment with the main union view and the static filter on; `meta` as configured, with its own index
   *  when it names one. */
  const envOf = (meta: Record<string, unknown> = META, extra: Record<string, string> = {}): Env => {
    const env = { ...deployment(), FILTER_STATIC: '1', STATIC_GEN: 'gen-primary', INDEX_R2: {} as R2Bucket, ...extra } as Env
    env.STORES_JSON = JSON.stringify({ ...JSON.parse(env.STORES_JSON!), meta })
    injectedStores.set(env, PRIMARY_INDEX)
    injectedStores.set(env, META_INDEX, 'meta')
    return env
  }
  const get = async (route: typeof subtree, env: Env, qs: string): Promise<[number, unknown]> => {
    const r = await route({ request: new Request(`http://localhost/api/x?w=1280&h=768&${qs}`), env })
    const text = await r.text()
    return [r.status, r.headers.get('content-type')?.startsWith('application/json') ? JSON.parse(text) : text]
  }
  const body = async (route: typeof subtree, env: Env, qs: string) => {
    const [status, b] = await get(route, env, qs)
    expect(status).toBe(200)
    return b as Body
  }
  const top = (t: Node) => (t.children ?? []).map(c => ({ ...c, children: undefined }))

  it('a member without its own index refuses for its mount: the answer is the others’, partial, the refusal named', async () => {
    const env = envOf()
    const [u, p] = [await body(subtree, env, `date=${A}&path=&q=ckpt`), await body(subtree, env, `date=${A}&path=&q=ckpt&store=primary`)]
    expect(u).toEqual({
      date: A, path: '', w: 1280, h: 768, minArea: 12, atten: 2,
      tier: p.tier, index: p.index,
      threshold: Math.round(filterThreshold(6000, 1280, 768, 12)),
      nodes: p.nodes, truncated: false,
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A, refused: NOT_INDEXED }] },
      q: 'ckpt',
      matches: ['bk/ckpt'], matched: [{ path: 'bk/ckpt', bytes: 6000, objects: 1 }], matchCount: { count: 1, bytes: 6000, objects: 1 },
      firstPaint: true,
      partial: true, partialReason: `meta: ${REJECT_MESSAGES['scan-not-indexed']}`,
      tree: { name: 'Fleet', kind: 'dir', bytes: 6000, objects: 1, children: (p.tree.children ?? []).map(c => ({ ...c, store: 'primary', scan: A })) },
    })
  })

  it('every member indexed: each answers for its own roots, under the union’s one threshold, merged', async () => {
    const env = envOf(META_OWN)
    puts.length = 0
    const u = await body(subtree, env, `date=${A}&path=&q=both`)
    // Each member read under its own budget, then again under the union's.
    const T = String(filterThreshold(6000 + 4194304, 1280, 768, 12))
    expect(reads()).toEqual([['meta', null], ['meta', T], ['primary', null], ['primary', T]])
    expect({ ...u, tree: { ...u.tree, children: top(u.tree) } }).toEqual({
      date: A, path: '', w: 1280, h: 768, minArea: 12, atten: 2,
      tier: u.tier, index: u.index,
      threshold: Math.round(filterThreshold(6000 + 4194304, 1280, 768, 12)),
      nodes: u.nodes, truncated: false,
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A }] },
      q: 'both',
      matches: ['bk/ckpt', 'mb/scans'],
      matched: [{ path: 'mb/scans', bytes: 4194304, objects: 1 }, { path: 'bk/ckpt', bytes: 6000, objects: 1 }],
      matchCount: { count: 2, bytes: 4194304 + 6000, objects: 2 },
      firstPaint: true,
      tree: {
        name: 'Fleet', kind: 'dir', bytes: 4194304 + 6000, objects: 2,
        children: [{ name: 'mb', kind: 'dir', bytes: 4194304, objects: 1, store: 'meta', scan: A, children: undefined }, { name: 'bk', kind: 'dir', bytes: 6000, objects: 1, store: 'primary', scan: A, children: undefined }],
      },
    })
    // A member with no match contributes nothing; no refusal, nothing partial.
    const only = await body(subtree, env, `date=${A}&path=&q=ckpt`)
    expect([only.matched, only.matchCount, only.partial, (only.union as { sources: unknown }).sources])
      .toEqual([[{ path: 'bk/ckpt', bytes: 6000, objects: 1 }], { count: 1, bytes: 6000, objects: 1 }, undefined, [{ store: 'primary', date: A }, { store: 'meta', date: A }]])
  })

  it('every member refusing: the first refusal, as its member gave it', async () => {
    // At C the primary's scan (C) is outside its index, meta's (B) has none.
    expect(await get(subtree, envOf(), `date=${C}&path=&q=ckpt`)).toEqual([400, NOT_INDEXED])
  })

  it('below the root, the path’s member answers — its refusal or its matches, byte for byte', async () => {
    for (const [meta, path, q, store] of [[META, 'mb', 'both', 'meta'], [META_OWN, 'mb', 'both', 'meta'], [META, 'bk', 'ckpt', 'primary']] as const) {
      const env = envOf(meta)
      expect(await get(subtree, env, `date=${A}&path=${path}&q=${q}`)).toEqual(await get(subtree, env, `date=${A}&path=${path}&q=${q}&store=${store}`))
    }
  })

  it('`?f=` drilled into one member’s mount: series and diff delegate to that member, `q=` and all', async () => {
    // The page opens a filtered union root with one matching root inside it (`r2://marin-na` on r2): the series
    // and the diff ask that root's path with `q=`, which its member answers as `store=<member>` would.
    const env = envOf(META_OWN)
    for (const [route, qs, store] of [
      [series, 'path=mb&q=both', 'meta'],
      [diff, `from=${A}&to=${B}&path=mb&q=both`, 'meta'],
      [series, 'path=bk&q=ckpt', 'primary'],
    ] as const) {
      const [s, b] = await get(route as typeof subtree, env, qs)
      expect([s, b]).toEqual(await get(route as typeof subtree, env, `${qs}&store=${store}`))
      expect(s).toBe(200)
    }
  })

  type DiffBody = Body & { rows: { path: string; depth: number }[]; bytes_before: number; bytes_after: number; objects_before: number; objects_after: number; expansions: number; lookups: number; threshold: number; matched: { path: string; bytes: number; objects: number }[]; matchCount: { count: number; bytes: number; objects: number } }
  /** A member's filtered diff's filter fields that the union root carries as they are (a partial reason is named). */
  const filterFieldsOf = (b: DiffBody) => Object.fromEntries(Object.entries(b).filter(([k]) => ['interiors', 'dirsOnly', 'matchesCapped', 'hexRuns'].includes(k)))
  const tagged = (b: DiffBody, store: string) => b.rows.map(r => (r.depth === 1 && r.path !== '(other)' ? { ...r, store } : r))

  it('the root’s filtered diff: each member diffs its own scans with `q=`; an unchanged one adds its matches to both sides, no rows', async () => {
    // A→B: the primary didn't rescan (A at both cutoffs: unchanged), meta did (A→B).
    const env = envOf(META_OWN)
    const m = await body(diff, env, `from=${A}&to=${B}&path=&q=both&store=meta`) as DiffBody
    const u = await body(diff, env, `from=${A}&to=${B}&path=&q=both`) as DiffBody
    // The unchanged member's self-diff walks its matches (its `expansions` / `lookups`) but draws nothing.
    expect({ ...u, expansions: u.expansions >= m.expansions, lookups: u.lookups >= m.lookups }).toEqual({
      prev: A, curr: B, path: '', q: 'both',
      rows: tagged(m, 'meta'),
      bytes_before: m.bytes_before + 6000, bytes_after: m.bytes_after + 6000, objects_before: m.objects_before + 1, objects_after: m.objects_after + 1,
      expansions: true, lookups: true,
      truncated: false,
      threshold: Math.round(filterThreshold(Math.max(m.bytes_before, m.bytes_after) + 6000, 1280, 768, 12)),
      tier: u.tier,
      lookups_capped: false,
      ...filterFieldsOf(m),
      matches: [],
      matched: [{ path: 'bk/ckpt', bytes: 6000, objects: 1 }, ...m.matched].sort((x, y) => y.bytes - x.bytes),
      matchCount: { count: m.matchCount.count + 1, bytes: m.matchCount.bytes + 6000, objects: m.matchCount.objects + 1 },
      union: { view: 'all', sources: [{ store: 'primary', from: A, to: A, change: 'unchanged' }, { store: 'meta', from: A, to: B, change: 'changed' }] },
    })
  })

  it('the root’s filtered diff: a refusing member is named, partial; every member refusing is that refusal', async () => {
    // A→C: the primary's C is outside its index (refuses); meta (A→B) answers with its own index, refuses without.
    const own = envOf(META_OWN)
    const m = await body(diff, own, `from=${A}&to=${B}&path=&q=both&store=meta`) as DiffBody
    const u = await body(diff, own, `from=${A}&to=${C}&path=&q=both`) as DiffBody
    expect(u).toEqual({
      prev: A, curr: C, path: '', q: 'both',
      rows: tagged(m, 'meta'),
      bytes_before: m.bytes_before, bytes_after: m.bytes_after, objects_before: m.objects_before, objects_after: m.objects_after,
      expansions: m.expansions, lookups: m.lookups,
      truncated: false,
      threshold: Math.round(filterThreshold(Math.max(m.bytes_before, m.bytes_after), 1280, 768, 12)),
      tier: m.tier,
      lookups_capped: false,
      ...filterFieldsOf(m),
      matches: [],
      matched: m.matched,
      matchCount: m.matchCount,
      partial: true, partialReason: `primary: ${REJECT_MESSAGES['scan-not-indexed']}`,
      union: { view: 'all', sources: [{ store: 'primary', from: A, to: C, change: 'changed', refused: NOT_INDEXED }, { store: 'meta', from: A, to: B, change: 'changed' }] },
    })
    expect(await get(diff, envOf(), `from=${A}&to=${C}&path=&q=both`)).toEqual([400, NOT_INDEXED])
  })

  it('the root still refuses one store’s scope axes, with or without a filter', async () => {
    expect(await get(diff, envOf(), `from=${A}&to=${B}&path=&q=ckpt&o=owned`)).toEqual([400, 'a union view\'s root is unscoped (o: drill into one store)'])
    expect(await get(subtree, envOf(), `date=${A}&path=&q=ckpt&o=owned`)).toEqual([400, 'a union view\'s root is unscoped (o: drill into one store)'])
  })

  it('`/api/filter-cover`: the members’ covers side by side; a refusing member makes it incomplete, named', async () => {
    const bare = envOf()
    const own = envOf(META_OWN)
    const [p, m] = [await body(filterCover, own, `date=${A}&path=&q=both&store=primary`), await body(filterCover, own, `date=${A}&path=&q=both&store=meta`)]
    expect(await body(filterCover, own, `date=${A}&path=&q=both`)).toEqual({
      date: A, path: '', q: 'both',
      items: [...(p.items as unknown[]), ...(m.items as unknown[])],
      roots: { count: 2, bytes: 4194304 + 6000, objects: 2 },
      complete: true,
      looked: (p.looked as number) + (m.looked as number), unchecked: 0,
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A }] },
    })
    expect(await body(filterCover, bare, `date=${A}&path=&q=ckpt`)).toEqual({
      date: A, path: '', q: 'ckpt',
      items: (await body(filterCover, bare, `date=${A}&path=&q=ckpt&store=primary`)).items,
      roots: { count: 1, bytes: 6000, objects: 1 },
      complete: false, reason: `meta: ${REJECT_MESSAGES['scan-not-indexed']}`,
      looked: p.looked, unchecked: 0,
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A, refused: NOT_INDEXED }] },
    })
    expect(await get(filterCover, bare, `date=${A}&path=mb&q=both`)).toEqual([400, NOT_INDEXED])
    expect(await get(filterCover, bare, `date=${C}&path=&q=ckpt`)).toEqual([400, NOT_INDEXED])
  })

  it('`/api/filter-caps`: indexed-only when any member is, under the deployment’s label', async () => {
    const caps = async (env: Env, qs: string) => (await filterCaps({ request: new Request(`http://localhost/api/filter-caps?${qs}`), env } as never)).json()
    const strictMeta = envOf({ ...META, vars: { ...META.vars, FILTER_INDEXED_ONLY: '1' } })
    expect([await caps(envOf(), ''), await caps(strictMeta, ''), await caps(strictMeta, 'store=primary'), await caps(strictMeta, 'store=meta')]).toEqual([
      { indexedOnly: false, rootLabel: 'Fleet' },
      { indexedOnly: true, rootLabel: 'Fleet' },
      { indexedOnly: false, rootLabel: 'Fleet' },
      { indexedOnly: true, rootLabel: 'our storage' },
    ])
  })

  it('`/api/filter-scans`: the cutoffs every member’s index covers, and each member’s own scans', async () => {
    const scans = async (env: Env) => (await filterScans({ request: new Request('http://localhost/api/filter-scans'), env } as never)).json()
    // Primary scans Z, A, C (index: A); meta A, B (index: A, B). Z: meta has no scan yet (doesn't block), the
    // primary's isn't indexed; A: both indexed; B: the primary's A and meta's B; C: the primary's isn't.
    expect([await scans(envOf(META_OWN)), await scans(envOf())]).toEqual([
      { scans: [A, B], union: { view: 'all', sources: [{ store: 'primary', scans: [A] }, { store: 'meta', scans: [A, B] }] } },
      { scans: [], union: { view: 'all', sources: [{ store: 'primary', scans: [A] }, { store: 'meta', scans: [] }] } },
    ])
  })
})
