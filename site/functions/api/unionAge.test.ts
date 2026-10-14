import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { LINEAGES } from '../_lib/testD1'
import { A, B, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as age } from './age-pyramid'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/age-pyramid` on a union view (specs/federated-scans.md, "Diffs and over-time"): the stores' own rows at
// their own scans, concatenated, each qualified by its store, bins absolute. The path store is the shared test
// deployment (`_lib/testUnion.ts`: the primary at Z, A, C; meta at A, B); its fixtures carry no age tiers, so the
// age reads (`age-pyramid-<bin>` handles and their point reads) are stubbed here per (store, scan, tier).

const H = 3_600_000
const DAY = 24 * H
const d0 = Date.parse('2026-06-01T00:00:00Z')
type Row = { path: string; depth: number; binstart: number; b: number; o: number }
const row = (binstart: number, b: number, o: number, path = '') => ({ path, depth: path ? path.split('/').length : 0, binstart, b, o })
/** (store, scan) → the finest (`1h`) tier's rows; a coarser tier's are these re-binned. */
const AGE: Record<string, Row[]> = {
  [`primary@${A}`]: [row(d0, 100, 1), row(d0 + DAY, 50, 1)],
  [`primary@${C}`]: [row(d0, 70, 1), row(d0 + H, 5, 1)],
  [`meta@${A}`]: [row(d0 + 12 * H, 9, 3), row(d0 + 2 * DAY, 1, 1)],
  [`meta@${B}`]: [row(d0 + 30 * DAY, 8, 2), row(d0 + 30 * DAY + H, 2, 1), row(d0 + 30 * DAY, 8, 2, 'mb'), row(d0 + 30 * DAY + H, 2, 1, 'mb')],
}
/** `ms`'s bin start in a tier of width `bin` (`<n>h` | `<n>d`), epoch-aligned. */
const binOf = (ms: number, bin: string): number => {
  const [, n, u] = /^(\d+)([hd])$/.exec(bin)!
  const w = Number(n) * (u === 'h' ? H : DAY)
  return Math.floor(ms / w) * w
}

vi.mock('../_lib/index.js', async orig => {
  const real = await orig<typeof import('../_lib/index.js')>()
  type Handle = { age: string; bin: string }
  return {
    ...real,
    openIndex: async (env: Env, date: string, variant?: string) => {
      if (!variant?.startsWith('age-pyramid-')) return real.openIndex(env, date, variant)
      const key = `${env.STORE_KEY ?? 'primary'}@${date}`
      if (!AGE[key]) throw new Error(`index ${variant} not synced for ${date}`)
      return { age: key, bin: variant.slice('age-pyramid-'.length) } as unknown as Awaited<ReturnType<typeof real.openIndex>>
    },
    readPoint: async (h: Handle, depth: number, path: string, cols?: string[]) => {
      if (!h.age) return real.readPoint(h as never, depth, path, cols)
      const by = new Map<number, Row>()
      for (const r of AGE[h.age].filter(r => r.depth === depth && r.path === path)) {
        const t = binOf(r.binstart, h.bin)
        const a = by.get(t)
        by.set(t, a ? { ...a, b: a.b + r.b, o: a.o + r.o } : { ...r, binstart: t })
      }
      return [...by.values()]
    },
  }
})

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    deployment = await unionDeployment(lineage)
  })

  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await age({ request: new Request(`http://localhost/api/age-pyramid?${qs}`), env: deployment(views) })
    return { status: r.status, text: await r.text() }
  }
  const body = async (qs: string, views?: Record<string, unknown>) => {
    const r = await get(qs, views)
    expect([r.status, r.status === 200 ? '' : r.text]).toEqual([200, ''])
    return JSON.parse(r.text) as { records: { dt: number; b: number; o: number; src?: string }[]; plan: { outputBin: string }; union?: unknown }
  }

  it('without a main view, the age rows are the primary’s alone, as before', async () => {
    expect((await get(`date=${A}&path=`, { all: { union: ['primary', 'meta'] } })).text).toBe((await get(`date=${A}&path=`, {})).text)
  })

  it('the root: each store’s rows at its own scan, qualified by store, bins absolute', async () => {
    // A: each store's own extent, and the union's, plan the finest tier at this budget — asked once.
    expect(await body(`date=${A}&path=&bin_budget=512`)).toEqual({
      records: [
        { dt: d0, b: 100, o: 1, src: 'primary' },
        { dt: d0 + 12 * H, b: 9, o: 3, src: 'meta' },
        { dt: d0 + DAY, b: 50, o: 1, src: 'primary' },
        { dt: d0 + 2 * DAY, b: 1, o: 1, src: 'meta' },
      ],
      plan: { outputBin: '1h', tier: '1h', binBudget: 512 },
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A }] },
    })
  })

  it('stores whose own extents plan different bins are asked again over the union’s extent: one bin for all', async () => {
    // C: the primary (C) spans an hour, meta (B, carried) an hour 30 days later: alone, each plans `1h`; together
    // a 30-day extent at a 40-bin budget plans `1d`, for both.
    const [u, p, m] = await Promise.all([body(`date=${C}&path=&bin_budget=40`), body(`date=${C}&path=&bin_budget=40&store=primary`), body(`date=${B}&path=&bin_budget=40&store=meta`)])
    expect([p.plan.outputBin, m.plan.outputBin]).toEqual(['1h', '1h'])
    expect(u).toEqual({
      records: [
        { dt: d0, b: 75, o: 2, src: 'primary' },
        { dt: d0 + 30 * DAY, b: 10, o: 3, src: 'meta' },
      ],
      plan: { outputBin: '1d', tier: '1d', binBudget: 40 },
      union: { view: 'all', at: '2026-10-03T00:01:00.000Z', sources: [{ store: 'primary', date: C }, { store: 'meta', date: B }] },
    })
  })

  it('a store unknown at the cutoff contributes no rows; a drill is the owning store’s own answer', async () => {
    expect(await body(`date=${Z}&path=`)).toEqual({
      records: [],
      plan: { outputBin: '1h', tier: '1h', binBudget: 512 },
      union: { view: 'all', at: '2026-09-30T00:01:00.000Z', sources: [{ store: 'primary', date: Z }, { store: 'meta', date: null }] },
    })
    expect((await get(`date=${C}&path=mb`)).text).toBe((await get(`date=${B}&path=mb&store=meta`)).text)
    expect(await get(`date=${A}&path=nope`)).toEqual({ status: 404, text: 'path not found' })
  })
})
