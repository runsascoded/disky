import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'

import { groupDiffRows } from '../_lib/groups'
import { A, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as diff } from './diff'
import { onRequestGet as groups } from './groups'
import { onRequestGet as series } from './series'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// Bucket groups (specs/bucket-groups.md, `_lib/groups.ts`): `?g=<name>` at the root is the store's root restricted to
// the group's buckets, composed from each member's own read. The deployment (`_lib/testUnion.ts`): the primary's roots
// `bk` and `zz` at Z and A, `bk` alone at C. Its groups: `both` (every root), `one` (`zz`, and `nope`, which no scan holds).

const GROUPS = { primary: { groups: { both: { label: 'both pools', buckets: ['zz', 'bk'] }, one: { label: 'just zz', buckets: ['zz', 'nope'] } } } }
const BOTH = { name: 'both', label: 'both pools', buckets: ['zz', 'bk'] }
const ONE = { name: 'one', label: 'just zz', buckets: ['zz', 'nope'] }

type Node = { name: string; kind: string; bytes: number; objects: number; children?: Node[] }
type Row = { path: string; depth: number; kind: string; status: string; bytes_before: number; bytes_after: number; objects_before: number; objects_after: number; expanded?: true }
type Pt = { date: string; bytes: number; objects: number }

describe('D1', () => {
  let env: Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    env = (await unionDeployment())(GROUPS)
  })

  const call = async (handler: (ctx: { request: Request; env: Env }) => Promise<Response>, url: string) => {
    const r = await handler({ request: new Request(`http://localhost${url}`), env })
    return { status: r.status, text: await r.text() }
  }
  const ok = async <T,>(handler: (ctx: { request: Request; env: Env }) => Promise<Response>, url: string): Promise<T> => {
    const r = await call(handler, url)
    expect([url, r.status, r.status === 200 ? '' : r.text]).toEqual([url, 200, ''])
    return JSON.parse(r.text) as T
  }
  /** A node with its children's top level (name, kind, bytes, objects), as the group lists its members. */
  const top = (n: Node) => ({ name: n.name, kind: n.kind, bytes: n.bytes, objects: n.objects, children: (n.children ?? []).map(({ name, kind, bytes, objects }) => ({ name, kind, bytes, objects })) })
  const view = (qs: string) => ok<{ tree: Node; tier: string; index: string; nodes: number } & Record<string, unknown>>(subtree, `/api/subtree?w=1280&h=768&${qs}`)
  /** A bucket's own node (its read at its own path). */
  const bucket = async (date: string, b: string) => {
    const { kind, bytes, objects } = (await view(`date=${date}&path=${b}`)).tree
    return { name: b, kind, bytes, objects }
  }

  it('`/api/groups`: the store\'s groups in config order; a store without any lists none', async () => {
    expect(await ok(groups, '/api/groups')).toEqual({ groups: [BOTH, ONE] })
    expect(await ok(groups, '/api/groups?store=meta')).toEqual({ groups: [] })
  })

  it('`/api/subtree`: a synthetic root labelled by the group, its members its children, their sums its own', async () => {
    const [bk, zz] = await Promise.all([bucket(A, 'bk'), bucket(A, 'zz')])
    const { tree, tier: _t, index: _i, nodes: _n, ...rest } = await view(`date=${A}&path=&g=both`)
    expect(rest).toEqual({ date: A, path: '', w: 1280, h: 768, minArea: 12, atten: 2, threshold: Math.round((bk.bytes + zz.bytes) * 12 / (1280 * 768)), truncated: false, group: BOTH })
    expect(top(tree)).toEqual({ name: 'both pools', kind: 'dir', bytes: bk.bytes + zz.bytes, objects: bk.objects + zz.objects, children: [bk, zz] })
    // Every root: the group's totals are the store root's.
    const root = (await view(`date=${A}&path=`)).tree
    expect([tree.bytes, tree.objects]).toEqual([root.bytes, root.objects])
  })

  it('a non-member is absent, and so is a member the scan doesn\'t hold', async () => {
    const zz = await bucket(A, 'zz')
    expect(top((await view(`date=${A}&path=&g=one`)).tree)).toEqual({ name: 'just zz', kind: 'dir', bytes: zz.bytes, objects: zz.objects, children: [zz] })
    // At C `zz` is gone too: the group is empty.
    expect(top((await view(`date=${C}&path=&g=one`)).tree)).toEqual({ name: 'just zz', kind: 'dir', bytes: 0, objects: 0, children: [] })
  })

  it('`depth=1`: the members alone, each a drillable branch without its children', async () => {
    const [bk, zz] = await Promise.all([bucket(A, 'bk'), bucket(A, 'zz')])
    const t = (await view(`date=${A}&path=&g=both&depth=1`)).tree
    expect(t.children!.map(c => ({ name: c.name, bytes: c.bytes, children: c.children ?? null }))).toEqual([
      { name: 'bk', bytes: bk.bytes, children: null },
      { name: 'zz', bytes: zz.bytes, children: null },
    ])
  })

  it('below the root `g` is ignored: the path\'s own answer', async () => {
    for (const path of ['bk', 'zz']) {
      const [g, own] = await Promise.all([call(subtree, `/api/subtree?w=1280&h=768&date=${A}&path=${path}&g=one`), call(subtree, `/api/subtree?w=1280&h=768&date=${A}&path=${path}`)])
      expect([path, g.status, g.text]).toEqual([path, 200, own.text])
    }
  })

  it('an unknown or malformed group is refused, on every route', async () => {
    for (const [route, url] of [[subtree, `/api/subtree?date=${A}&path=`], [diff, `/api/diff?from=${A}&to=${C}&path=`], [series, '/api/series?path=']] as const) {
      expect([await call(route, `${url}&g=nope`), await call(route, `${url}&g=No!`)]).toEqual([
        { status: 404, text: '{"error":"unknown group \'nope\'"}\n' },
        { status: 400, text: '{"error":"bad group"}\n' },
      ])
    }
  })

  const diffOf = (qs: string) => ok<{ rows: Row[]; bytes_before: number; bytes_after: number; objects_before: number; objects_after: number; group?: unknown; threshold: number }>(diff, `/api/diff?w=1280&h=768&${qs}`)
  const sides = (b: { bytes_before: number; bytes_after: number; objects_before: number; objects_after: number }) =>
    ({ bytes_before: b.bytes_before, bytes_after: b.bytes_after, objects_before: b.objects_before, objects_after: b.objects_after })

  it('`/api/diff` `summary=1`: the members\' totals summed, no rows; a member neither scan holds absent', async () => {
    const zz = await bucket(A, 'zz')
    const s = await diffOf(`from=${A}&to=${C}&path=&g=one&summary=1`)
    expect([s.rows, sides(s), s.group]).toEqual([[], { bytes_before: zz.bytes, bytes_after: 0, objects_before: zz.objects, objects_after: 0 }, ONE])
  })

  it('`groupDiffRows`: every member its depth-1 row, an unchanged one included at zero delta; a member\'s rows below it', () => {
    const row = { depth: 1, kind: 'dir' as const, status: 'added' as const, bytes_before: 0, bytes_after: 5, objects_before: 0, objects_after: 1 }
    expect(groupDiffRows([
      { bucket: 'hot', total: { bytes_before: 10, bytes_after: 15, objects_before: 2, objects_after: 3 }, rows: [{ ...row, path: 'new' }] },
      { bucket: 'still', total: { bytes_before: 7, bytes_after: 7, objects_before: 4, objects_after: 4 }, rows: [] },
    ])).toEqual([
      { path: 'hot', depth: 1, kind: 'dir', status: 'changed', bytes_before: 10, bytes_after: 15, objects_before: 2, objects_after: 3, expanded: true },
      { path: 'hot/new', depth: 2, kind: 'dir', status: 'added', bytes_before: 0, bytes_after: 5, objects_before: 0, objects_after: 1 },
      { path: 'still', depth: 1, kind: 'dir', status: 'unchanged', bytes_before: 7, bytes_after: 7, objects_before: 4, objects_after: 4 },
    ])
  })

  it('`/api/diff` `depth=1`: one row per member a scan holds, from its totals, an unchanged one included', async () => {
    const [zzA, bkA, bkC] = await Promise.all([bucket(A, 'zz'), bucket(A, 'bk'), bucket(C, 'bk')])
    expect((await diffOf(`from=${A}&to=${C}&path=&g=one&depth=1`)).rows).toEqual([
      { path: 'zz', depth: 1, kind: 'dir', status: 'removed', bytes_before: zzA.bytes, bytes_after: 0, objects_before: zzA.objects, objects_after: 0 },
    ])
    const both = await diffOf(`from=${A}&to=${C}&path=&g=both&depth=1`)
    expect(both.rows).toEqual([
      { path: 'zz', depth: 1, kind: 'dir', status: 'removed', bytes_before: zzA.bytes, bytes_after: 0, objects_before: zzA.objects, objects_after: 0 },
      { path: 'bk', depth: 1, kind: 'dir', status: bkA.bytes === bkC.bytes && bkA.objects === bkC.objects ? 'unchanged' : 'changed', bytes_before: bkA.bytes, bytes_after: bkC.bytes, objects_before: bkA.objects, objects_after: bkC.objects },
    ])
    // Z and A hold the same content: both members unchanged, each still its row, at zero delta.
    expect((await diffOf(`from=${Z}&to=${A}&path=&g=both&depth=1`)).rows).toEqual([
      { path: 'zz', depth: 1, kind: 'dir', status: 'unchanged', bytes_before: zzA.bytes, bytes_after: zzA.bytes, objects_before: zzA.objects, objects_after: zzA.objects },
      { path: 'bk', depth: 1, kind: 'dir', status: 'unchanged', bytes_before: bkA.bytes, bytes_after: bkA.bytes, objects_before: bkA.objects, objects_after: bkA.objects },
    ])
  })

  it('`/api/diff`: an unchanged member keeps its depth-1 row', async () => {
    const [zz, bk] = await Promise.all([bucket(A, 'zz'), bucket(A, 'bk')])
    const g = await diffOf(`from=${Z}&to=${A}&path=&g=both`)
    const below = (b: string) => g.rows.filter(r => r.path.startsWith(`${b}/`)).length > 0 ? { expanded: true as const } : {}
    expect(g.rows.filter(r => r.depth === 1)).toEqual([
      { path: 'zz', depth: 1, kind: 'dir', status: 'unchanged', bytes_before: zz.bytes, bytes_after: zz.bytes, objects_before: zz.objects, objects_after: zz.objects, ...below('zz') },
      { path: 'bk', depth: 1, kind: 'dir', status: 'unchanged', bytes_before: bk.bytes, bytes_after: bk.bytes, objects_before: bk.objects, objects_after: bk.objects, ...below('bk') },
    ])
  })

  it('`/api/diff`: every root, the store root\'s totals; each member\'s rows below its own, one level deeper', async () => {
    const [g, root] = await Promise.all([diffOf(`from=${A}&to=${C}&path=&g=both`), diffOf(`from=${A}&to=${C}&path=`)])
    expect(sides(g)).toEqual(sides(root))
    // Each member's rows reparented under its bucket row (read at the group's threshold, so compared by shape): a row's
    // depth is its path's, every row is under a member, and a member row is expanded exactly when rows sit under it.
    const below = (b: string) => g.rows.filter(r => r.path.startsWith(`${b}/`)).length > 0 ? true : null
    expect(g.rows.filter(r => r.depth === 1).map(r => [r.path, r.status, r.expanded ?? null])).toEqual([
      ['zz', 'removed', below('zz')],
      ['bk', 'changed', below('bk')],
    ])
    expect(g.rows.filter(r => r.depth !== r.path.split('/').length || !['zz', 'bk'].includes(r.path.split('/')[0]))).toEqual([])
  })

  const seriesOf = (qs: string) => ok<{ points: Pt[]; roots?: { path: string; points: Pt[] }[]; group?: unknown }>(series, `/api/series?${qs}`)

  it('`/api/series`: the members\' series summed per scan; a member absent from a scan adds nothing', async () => {
    const [bk, zz] = await Promise.all([seriesOf('path=bk'), seriesOf('path=zz')])
    expect([bk.points.map(p => p.date), zz.points.map(p => p.date)]).toEqual([[Z, A, C], [Z, A]])
    const at = (s: { points: Pt[] }, d: string) => s.points.find(p => p.date === d) ?? { bytes: 0, objects: 0 }
    expect(await seriesOf('path=&g=both')).toEqual({
      path: '',
      points: [Z, A, C].map(date => ({ date, bytes: at(bk, date).bytes + at(zz, date).bytes, objects: at(bk, date).objects + at(zz, date).objects })),
      group: BOTH,
    })
    expect(await seriesOf('path=&g=one')).toEqual({ path: '', points: zz.points, group: ONE })
  })

  it('`/api/series` `split=roots`: the root\'s per-bucket traces, the members\' kept and summed', async () => {
    const root = await seriesOf('path=&split=roots')
    const zz = root.roots!.find(t => t.path === 'zz')!
    expect(await seriesOf('path=&split=roots&g=one')).toEqual({ path: '', points: zz.points, roots: [zz], group: ONE })
  })

  it('`/api/series` `paths=` (a filter\'s match roots): every one under a member, else refused', async () => {
    expect((await call(series, '/api/series?path=&g=one&paths=zz')).text).toBe((await call(series, '/api/series?path=&paths=zz')).text)
    expect(await call(series, '/api/series?path=&g=one&paths=bk')).toEqual({ status: 400, text: '{"error":"paths outside group \'one\'"}\n' })
  })

  it('a union view\'s root takes no group', async () => {
    const u = { ...GROUPS, all: { union: ['primary', 'meta'], main: true } }
    const uenv = (await unionDeployment())(u)
    const r = await subtree({ request: new Request(`http://localhost/api/subtree?date=${A}&path=&g=both`), env: uenv })
    expect([r.status, await r.text()]).toEqual([400, 'a union view\'s root is unscoped (g: drill into one store)'])
  })
})
