import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { LINEAGES } from '../_lib/testD1'
import { A, B, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as diff, unionDiffRows } from './diff'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/diff` on a union view (specs/federated-scans.md, "Diffs and over-time"): both cutoffs resolve
// independently, each store diffs its own before/after scans; a store that didn't rescan contributes zero, a
// store with no scan by a cutoff is unknown — left out, never added or deleted. The deployment
// (`_lib/testUnion.ts`): the primary scans at Z, A, C; meta at A, B.

type Row = { p: string; d: number; k: string; s: string; a: number; b: number; oa: number; ob: number; src?: string }
type Body = { prev: string; curr: string; rows: Row[]; total_a: number; total_b: number; objects_a: number; objects_b: number; threshold: number; union?: { view: string; sources: unknown[] } }

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    deployment = await unionDeployment(lineage)
  })

  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await diff({ request: new Request(`http://localhost/api/diff?w=1280&h=768&${qs}`), env: deployment(views) })
    return { status: r.status, text: await r.text() }
  }
  const body = async (qs: string, views?: Record<string, unknown>) => {
    const r = await get(qs, views)
    expect([r.status, r.status === 200 ? '' : r.text]).toEqual([200, ''])
    return JSON.parse(r.text) as Body
  }
  const roots = (b: Body) => b.rows.filter(r => r.d === 1).map(r => [r.p, r.src ?? null, r.s])
  const totals = (bs: Body[]) => ({
    total_a: bs.reduce((n, b) => n + b.total_a, 0), total_b: bs.reduce((n, b) => n + b.total_b, 0),
    objects_a: bs.reduce((n, b) => n + b.objects_a, 0), objects_b: bs.reduce((n, b) => n + b.objects_b, 0),
  })
  const pick = (b: Body) => ({ total_a: b.total_a, total_b: b.total_b, objects_a: b.objects_a, objects_b: b.objects_b })
  /** Meta's root total at a scan, as its own subtree says. */
  const meta = async (date: string) => {
    const r = await subtree({ request: new Request(`http://localhost/api/subtree?date=${date}&path=&store=meta`), env: deployment() })
    const t = ((await r.json()) as { tree: { b: number; o: number } }).tree
    return { b: t.b, o: t.o }
  }

  it('without a main view, the diff is the primary’s alone, as before', async () => {
    expect((await get(`from=${A}&to=${C}&path=`, { all: { union: ['primary', 'meta'] } })).text).toBe((await get(`from=${A}&to=${C}&path=`, {})).text)
  })

  it('both stores rescanned: each diffs its own scans; rows tagged, totals summed, one threshold', async () => {
    const [u, p, m] = await Promise.all([body(`from=${A}&to=${C}&path=`), body(`from=${A}&to=${C}&path=&store=primary`), body(`from=${A}&to=${B}&path=&store=meta`)])
    expect([u.prev, u.curr, u.union, roots(u), pick(u), u.threshold]).toEqual([
      A, C,
      { view: 'all', sources: [{ store: 'primary', from: A, to: C, change: 'changed' }, { store: 'meta', from: A, to: B, change: 'changed' }] },
      [...roots(p).map(([n, , s]) => [n, 'primary', s]), ...roots(m).map(([n, , s]) => [n, 'meta', s])],
      totals([p, m]),
      Math.round(Math.max(p.total_a + m.total_a, p.total_b + m.total_b) * 12 / (1280 * 768)),
    ])
    // The primary's `bk` changed at C (its 30-byte `zz` went, under the floor); meta's content didn't move: no rows.
    expect([roots(u), roots(m)]).toEqual([[['bk', 'primary', 'changed']], []])
  })

  it('a store that didn’t rescan contributes zero: no rows, its root total on both sides', async () => {
    // B → C: the primary A → C; meta's latest is B at both cutoffs.
    const [u, p, mb] = await Promise.all([body(`from=${B}&to=${C}&path=`), body(`from=${A}&to=${C}&path=&store=primary`), meta(B)])
    expect([u.union, roots(u), pick(u)]).toEqual([
      { view: 'all', sources: [{ store: 'primary', from: A, to: C, change: 'changed' }, { store: 'meta', from: B, to: B, change: 'unchanged' }] },
      roots(p).map(([n, , s]) => [n, 'primary', s]),
      { total_a: p.total_a + mb.b, total_b: p.total_b + mb.b, objects_a: p.objects_a + mb.o, objects_b: p.objects_b + mb.o },
    ])
  })

  it('a store with no scan by a cutoff is unknown: absent from rows and totals, named — never added', async () => {
    // Z → A: meta's first scan is A.
    const [u, p] = await Promise.all([body(`from=${Z}&to=${A}&path=`), body(`from=${Z}&to=${A}&path=&store=primary`)])
    expect([u.union, roots(u), pick(u)]).toEqual([
      { view: 'all', sources: [{ store: 'primary', from: Z, to: A, change: 'changed' }, { store: 'meta', from: null, to: A, change: 'unknown' }] },
      roots(p).map(([n, , s]) => [n, 'primary', s]),
      pick(p),
    ])
    expect(await get(`from=${Z}&to=${A}&path=mb`)).toEqual({
      status: 409,
      text: JSON.stringify({ error: `store 'meta' has no scan by ${Z}: its change is unknown`, union: u.union }) + '\n',
    })
  })

  it('below the root: the owning store’s own diff of its own scans', async () => {
    expect((await get(`from=${A}&to=${C}&path=mb`)).text).toBe((await get(`from=${A}&to=${B}&path=mb&store=meta`)).text)
    expect((await get(`from=${Z}&to=${C}&path=bk`)).text).toBe((await get(`from=${Z}&to=${C}&path=bk&store=primary`)).text)
    // B → C under `mb`: meta didn't rescan — its one scan against itself: equal totals, nothing moved.
    const same = await body(`from=${B}&to=${C}&path=mb`)
    const mb = await meta(B)
    expect([same.prev, same.curr, pick(same), same.rows]).toEqual([B, B, { total_a: mb.b, total_b: mb.b, objects_a: mb.o, objects_b: mb.o }, []])
    expect(await get(`from=${A}&to=${C}&path=nope`)).toEqual({ status: 404, text: 'path not found in either scan' })
  })

  it('cutoffs out of order, unindexed, or a scoped root are refused', async () => {
    expect([await get(`from=${C}&to=${A}&path=`), await get(`from=${A}&to=2026-10-09T0001&path=`), await get(`from=${A}&to=${C}&path=&o=unowned`)]).toEqual([
      { status: 400, text: 'from must precede to' },
      { status: 404, text: '{"error":"no scan matches to=2026-10-09T0001"}\n' },
      { status: 400, text: 'a union view\'s root is unscoped (o: drill into one store)' },
    ])
  })
})

describe('unionDiffRows', () => {
  const view = { key: 'all', members: ['primary', 'meta'], main: true }
  const row = (p: string, d: number, a: number, b: number, s = a === b ? 'unchanged' : 'changed') => ({ p, d, k: 'dir' as const, s: s as 'changed', a, b, oa: 1, ob: 1 })
  it('tags depth-1 rows, keeps deeper ones, sums the `(other)` folds (restating their status)', () => {
    expect(unionDiffRows(view, [
      { store: 'primary', rows: [row('a', 1, 5, 9), row('a/x', 2, 5, 9), row('(other)', 1, 2, 3)] },
      { store: 'meta', rows: [row('m', 1, 4, 4), row('(other)', 1, 3, 2)] },
    ])).toEqual([
      { ...row('a', 1, 5, 9), src: 'primary' },
      row('a/x', 2, 5, 9),
      { ...row('m', 1, 4, 4), src: 'meta' },
      { ...row('(other)', 1, 5, 5), oa: 2, ob: 2, s: 'unchanged' },
    ])
  })
  it('refuses a depth-1 name two stores draw', () => {
    expect(() => unionDiffRows(view, [{ store: 'primary', rows: [row('a', 1, 1, 1)] }, { store: 'meta', rows: [row('a', 1, 1, 1)] }])).toThrow("union view 'all': root 'a' is in stores primary, meta")
  })
})
