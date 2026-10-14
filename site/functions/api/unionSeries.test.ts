import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { LINEAGES } from '../_lib/testD1'
import { A, B, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as series } from './series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/series` on a union view (specs/federated-scans.md, "Diffs and over-time"): the members' own series as one
// step function — at each instant any member scanned, each carries its latest point; before its first, it is
// missing (the total partial), never zero. The deployment (`_lib/testUnion.ts`): the primary scans at Z, A, C;
// meta at A, B.

type Pt = { date: string; b: number; o: number; missing?: string[] }
type Body = { path: string; points: Pt[]; roots?: { path: string; points: Pt[] }[]; union?: unknown }

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    deployment = await unionDeployment(lineage)
  })

  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await series({ request: new Request(`http://localhost/api/series?${qs}`), env: deployment(views) })
    return { status: r.status, text: await r.text() }
  }
  const body = async (qs: string, views?: Record<string, unknown>) => {
    const r = await get(qs, views)
    expect([r.status, r.status === 200 ? '' : r.text]).toEqual([200, ''])
    return JSON.parse(r.text) as Body
  }

  it('without a main view, the root series is the primary’s alone, as before', async () => {
    expect((await get('path=', { all: { union: ['primary', 'meta'] } })).text).toBe((await get('path=', {})).text)
  })

  it('the root: one step per union event, each store’s latest point carried, a store before its first named missing', async () => {
    const [u, p, m] = await Promise.all([body('path='), body('path=&store=primary'), body('path=&store=meta')])
    const at = (b: Body, d: string) => b.points.find(x => x.date === d)!
    const sum = (x: Pt, y: Pt) => ({ b: x.b + y.b, o: x.o + y.o })
    expect([p.points.map(x => x.date), m.points.map(x => x.date)]).toEqual([[Z, A, C], [A, B]])
    expect(u).toEqual({
      path: '',
      points: [
        { date: Z, b: at(p, Z).b, o: at(p, Z).o, missing: ['meta'] },
        { date: A, ...sum(at(p, A), at(m, A)) },
        { date: B, ...sum(at(p, A), at(m, B)) },
        { date: C, ...sum(at(p, C), at(m, B)) },
      ],
      union: { view: 'all', sources: [{ store: 'primary', from: Z }, { store: 'meta', from: A }] },
    })
  })

  it('`split=roots`: every store’s roots, each at its store’s carried point, a root starting at its store’s first scan', async () => {
    const [u, p, m] = await Promise.all([body('path=&split=roots'), body('path=&split=roots&store=primary'), body('path=&split=roots&store=meta')])
    const trace = (b: Body, root: string) => b.roots!.find(r => r.path === root)!.points
    const pt = (date: string, r: Pt) => ({ date, b: r.b, o: r.o })
    const [bk, zz, mb] = [trace(p, 'bk'), trace(p, 'zz'), trace(m, 'mb')]
    // The primary's `zz` is gone at C; its `bk` changed. Meta's `mb` carries A, then B.
    expect([bk.map(x => x.date), zz.map(x => x.date), mb.map(x => x.date)]).toEqual([[Z, A, C], [Z, A], [A, B]])
    expect(u.roots).toEqual([
      { path: 'bk', points: [pt(Z, bk[0]), pt(A, bk[1]), pt(B, bk[1]), pt(C, bk[2])] },
      { path: 'zz', points: [pt(Z, zz[0]), pt(A, zz[1]), pt(B, zz[1])] },
      { path: 'mb', points: [pt(A, mb[0]), pt(B, mb[1]), pt(C, mb[1])] },
    ])
    expect(u.points.map(x => [x.date, x.b, x.missing ?? null])).toEqual([
      [Z, bk[0].b + zz[0].b, ['meta']],
      [A, bk[1].b + zz[1].b + mb[0].b, null],
      [B, bk[1].b + zz[1].b + mb[1].b, null],
      [C, bk[2].b + mb[1].b, null],
    ])
  })

  it('below the root: the owning store’s own series', async () => {
    for (const [path, store] of [['mb', 'meta'], ['mb/scans', 'meta'], ['bk', 'primary']] as const) {
      const [u, own] = await Promise.all([get(`path=${path}`), get(`path=${path}&store=${store}`)])
      expect([path, u.status, u.text]).toEqual([path, 200, own.text])
    }
    expect(await get('path=nope')).toEqual({ status: 404, text: '{"error":"path not found"}\n' })
  })

  it('the root refuses one store’s scope axes', async () => {
    expect(await get('path=&o=unowned&paths=bk')).toEqual({ status: 400, text: '{"error":"a union view\'s root is unscoped (o, paths: drill into one store)"}\n' })
  })
})
