import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { coarsenRects, type Span } from './index'
import { fixture, ivClassBytes } from './testStore'
import { sqliteD1 } from './testD1'
import { buildDiff, buildView } from './view'

/** The planned group each sort's plan is inflated by (`null`: planned as is), per scan: past the read budget by its
 *  rows, with a `bMax` saying how far a floor must rise to leave it out. What the real store's plans hold: `bysize`
 *  has at least a group per size bucket, per segment, per tier, whatever P holds. The fixture is too small to reach
 *  the budget itself. The heavy group is never fetched: a floor over its `bMax` drops it, and otherwise the read is
 *  refused before any data is. */
const planning = vi.hoisted(() => ({
  size: new Map<string, { bMax: number; rows: number }>(),
  path: new Map<string, { bMax: number; rows: number }>(),
  sizeCalls: [] as string[],
  sizeReads: [] as string[],
  wideRects: 0,
}))

vi.mock('./index', async importOriginal => {
  const actual = await importOriginal<typeof import('./index')>()
  const heavy = (spans: Span[], o?: { bMax: number; rows: number }): Span[] =>
    o ? [...spans, { rg: 1_000_000, dMin: 3, dMax: 3, pMin: 'b2/e/', pMax: 'b2/e0', bMax: o.bMax, rowStart: 0, rowEnd: o.rows }] : spans
  return {
    ...actual,
    planSizeRects: async (...args: Parameters<typeof actual.planSizeRects>): Promise<Span[]> => {
      planning.sizeCalls.push(args[0].date)
      return heavy(await actual.planSizeRects(...args), planning.size.get(args[0].date))
    },
    planRects: async (...args: Parameters<typeof actual.planRects>): Promise<Span[]> => heavy(await actual.planRects(...args), planning.path.get(args[0].date)),
    readSizeRects: async (...args: Parameters<typeof actual.readSizeRects>) => {
      planning.sizeReads.push(args[0].date)
      return actual.readSizeRects(...args)
    },
    readRects: async (...args: Parameters<typeof actual.readRects>) => {
      if (planning.wideRects > 0) {
        planning.wideRects--
        throw new Error('query too wide: drill deeper or raise minArea')
      }
      return actual.readRects(...args)
    },
  }
})
vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const REFUSED = 'query too wide: drill deeper or raise minArea'
/** A floor just past `x`, as `coarsen` sets it (`RAISE_PAST`). */
const past = (x: number) => x * (1 + 1e-9)

async function storeEnv(): Promise<Env> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as {
    existsSync(path: string): boolean
    readFileSync(path: string): Uint8Array
  }
  const bucket = {
    get: async (key: string, o?: { range?: { offset: number; length: number } }) => {
      const file = fixture(`iv/${key}`)
      if (!fs.existsSync(file)) return null
      const buf = fs.readFileSync(file)
      const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
      const part = buf.slice(offset, offset + length)
      return {
        size: buf.byteLength,
        arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
        json: async () => JSON.parse(new TextDecoder().decode(part)),
      }
    },
  } as unknown as R2Bucket
  const { db } = await sqliteD1()
  return { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket } as Env
}

beforeEach(() => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  planning.size.clear()
  planning.path.clear()
  planning.sizeCalls.length = 0
  planning.sizeReads.length = 0
  planning.wideRects = 0
})

const VIEW = { date: '2026-08-03', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0 }
const DIFF = { from: '2026-08-04', to: '2026-08-05', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, top: 100, ivPlanRows: 0 }

describe('a view whose plan is past the read budget', () => {
  it('is read at the lowest floor that fits, and is what that floor gets', async () => {
    const env = await storeEnv()
    // b2/e's total × 12 / (8 × 6) = 5: the heavy group's `bMax` of 15 clears it 3× on either sort (a tie: `bysize`).
    planning.size.set(VIEW.date, { bMax: 15, rows: 700_001 })
    planning.path.set(VIEW.date, { bMax: 15, rows: 700_001 })
    const view = await buildView(env, VIEW)
    expect(view.threshold).toBe(past(15))
    expect(view.tier).toBe('bysize')
    const cb = await ivClassBytes(VIEW.date, VIEW.path, VIEW.w)
    expect(view.tree).toEqual({
      n: 'e', k: 'dir', b: 20, o: 3, a: 6, t: 7, d: 0, cb: cb[''], us: [['alice', 10]],
      c: [
        { n: 'd.json', k: 'file', b: 20, o: 1, a: 6, t: 7, d: 0, cb: cb['/d.json'] },
      ],
    })
    // The same answer as asking for that floor outright, on a plan that fits.
    planning.size.clear()
    planning.path.clear()
    expect(await buildView(env, { ...VIEW, threshold: past(15) })).toEqual(view)
  })

  it('keeps its floor when another sort fits: the exact answer beats a coarser one', async () => {
    const env = await storeEnv()
    planning.size.set(VIEW.date, { bMax: 15, rows: 700_001 })
    const view = await buildView(env, VIEW)
    expect([view.threshold, view.tier]).toEqual([5, 'path'])
  })

  it('still refuses when its heaviest group alone is past the budget: no floor helps', async () => {
    const env = await storeEnv()
    planning.size.set(VIEW.date, { bMax: 1_000_000, rows: 700_001 })
    planning.path.set(VIEW.date, { bMax: 1_000_000, rows: 700_001 })
    await expect(buildView(env, VIEW)).rejects.toThrow(new Error(REFUSED))
  })

  it('under a lens stays within the budget too: what the lens gets at that floor, its total exact', async () => {
    const env = { ...await storeEnv(), INTERVAL_STORE_GEN: 'g2' } as Env
    const o = { date: '2026-09-30T0007', path: 'bk', w: 256, h: 128, minArea: 12, atten: 2, ivPlanRows: 0, lens: { key: 'alice' } }
    const plain = await buildView(env, o)
    planning.size.set(o.date, { bMax: plain.threshold * 3, rows: 700_001 })
    planning.path.set(o.date, { bMax: plain.threshold * 3, rows: 700_001 })
    const view = await buildView(env, o)
    // The heavy group sits a level below the read's top, where `path` holds it to twice the floor: 1.5× is enough there
    // (3× on `bysize`, whose one floor is the top level's), and the sort needing the smaller rise answers.
    expect([view.threshold / plain.threshold, view.tier]).toEqual([expect.closeTo(1.5, 6), 'path'])
    expect(view.tree.b).toBe(plain.tree.b)
    planning.size.clear()
    planning.path.clear()
    // At that floor the sort it would pick unprompted answers the same tree.
    expect(await buildView(env, { ...o, threshold: view.threshold })).toEqual({ ...view, tier: 'bysize-user' })
  })
})

describe('a diff whose plan is past the read budget', () => {
  it('reads both sides once, at the raised floor: no name on one side and folded on the other', async () => {
    const env = await storeEnv()
    planning.size.set(DIFF.to, { bMax: 15, rows: 700_001 })
    planning.path.set(DIFF.to, { bMax: 15, rows: 700_001 })
    const diff = await buildDiff(env, DIFF)
    const raised = diff.threshold
    expect(raised).toBeGreaterThan(0)
    // The side under the raised floor (`from`) plans again, in memory; nothing decodes twice.
    expect(planning.sizeCalls.sort()).toEqual([DIFF.from, DIFF.from, DIFF.to])
    expect(planning.sizeReads.sort()).toEqual([DIFF.from, DIFF.to])
    planning.size.clear()
    planning.path.clear()
    expect(await buildDiff(env, { ...DIFF, threshold: raised })).toEqual(diff)
  })

  it('a descent too wide at a level hands off to the walk, whose floor can rise', async () => {
    const env = await storeEnv()
    planning.wideRects = 2
    const diff = await buildDiff(env, { ...DIFF, descent: true })
    expect(planning.wideRects).toBe(0)
    expect(await buildDiff(env, DIFF)).toEqual(diff)
  })
})

describe('coarsenRects', () => {
  const span = (rg: number, bMax: number, rows: number): Span => ({ rg, dMin: 2, dMax: 2, pMin: 'a', pMax: 'b', bMax, rowStart: 0, rowEnd: rows })
  const rects = [{ dLo: 2, dHi: 1e9, pLo: 'a', pHi: 'b' }]
  const flat = () => 10

  it('takes the heaviest groups the row budget holds, whatever the order they came in', () => {
    const plan = [span(0, 40, 300_000), span(1, 80, 300_000), span(2, 20, 300_000), span(3, 10, 300_000)]
    const want = { spans: [span(0, 40, 300_000), span(1, 80, 300_000)], raise: past(2) }
    expect(coarsenRects(plan, rects, flat)).toEqual(want)
    expect(coarsenRects([...plan].reverse(), rects, flat)).toEqual(want)
  })

  it('takes at most the group budget', () => {
    const plan = Array.from({ length: 252 }, (_, i) => span(i, 1000 - i, 1))
    const got = coarsenRects(plan, rects, flat)!
    expect([got.spans.length, got.spans.at(-1)!.rg, got.raise]).toEqual([250, 249, past(75)])
  })

  it('leaves out groups tied with the first it can\'t take: a floor can\'t part them', () => {
    const plan = [span(0, 80, 400_000), span(1, 40, 200_000), span(2, 40, 200_000)]
    expect(coarsenRects(plan, rects, flat)).toEqual({ spans: [span(0, 80, 400_000)], raise: past(4) })
  })

  it('scales each group\'s own floor: a group at depth is held to its attenuated threshold', () => {
    const at = (d: number) => 10 * 2 ** (d - 2)
    const deep = { ...span(0, 80, 400_000), dMin: 4, dMax: 4 } // clears 80 / 40 = 2×
    const shallow = span(1, 50, 400_000) // clears 50 / 10 = 5×
    expect(coarsenRects([deep, shallow], rects, at)).toEqual({ spans: [shallow], raise: past(2) })
  })

  it('a plan that fits is untouched; no floor helps past the budget in its heaviest group, or with no floor to scale', () => {
    const plan = [span(0, 80, 400_000)]
    expect(coarsenRects(plan, rects, flat)).toEqual({ spans: plan, raise: 1 })
    expect(coarsenRects([span(0, 80, 700_001), span(1, 20, 1)], rects, flat)).toBe(null)
    expect(coarsenRects(plan, rects, () => 0)).toBe(null)
  })
})
