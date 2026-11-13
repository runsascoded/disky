import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { type Ask, openIndex, readAsks, withTrace } from './index'
import { sqliteD1 } from './testD1'
import { fixtureR2 } from './testR2'
import { buildDiff } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// What a diff's lookups read, over `fixtures/iv/`'s interval store (`g1`: six scans, 4 rows per group; see
// `intervalStore.test.ts`). As of 2026-08-03 its `path` sort's live groups are one segment's:
//
//   21  depths 1–2   b1, b1/d, b1/gof, b2/e
//   22  depths 2–3   b2/f · b1/d/a.txt, b1/d/b.pq, b1/d/e
//   23  depth 3      b1/d/f.bin … b1/gof/f.bin
//   24  depth 3      b2/e/d.json … b2/f/b.pq
//   25  depth 3      b2/f/c, b2/f/d.json
//
// The walk's lookups (`buildDiff`) are a round's names, heaviest first: many depths, all over the tree.

let env: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1()
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: fixtureR2('iv', []) } as Env
})

describe('a diff\'s lookups read', () => {
  /** `asks` as of 2026-08-03: the span selections made, the groups read, and the rows as `[depth, path, size]`. */
  const read = async (asks: Ask[], points: boolean) => {
    const h = await openIndex(env, '2026-08-03', 'path')
    const want = new Set(asks.map(a => `${a.depth}\0${'path' in a ? a.path : ''}`))
    let spans = 0
    const got = await readAsks(withTrace(h, n => { if (n === 'askspan') spans++ }), asks, r => want.has(`${r.depth}\0${r.path}`), { points })
    return { spans, groups: got.groups, rows: got.rows.map(r => [r.depth, r.path, r.size]) }
  }

  it('by their points: one span selection whatever their depths, and no group that holds none of them', async () => {
    // Three depths (`b2` has no version live then): a rectangle per depth is three selections; the points are one.
    const depths: Ask[] = [{ depth: 1, path: 'b2' }, { depth: 2, path: 'b1/gof' }, { depth: 3, path: 'b2/f/c' }]
    const depthsRows = [[2, 'b1/gof', 0], [3, 'b2/f/c', 0]]
    // `b1/a` (no such path) sorts before every path of depth 3: the rectangle `[b1/a, b2/f/c]` takes in group 22,
    // which straddles the depth, and a group straddling depths is read whatever paths it holds (`groupMayHold`). Its
    // paths of depth 3 run from `b1/d/a.txt` to `b1/d/f.bin` (group 23's first): neither point is one.
    const straddle: Ask[] = [{ depth: 3, path: 'b1/a' }, { depth: 3, path: 'b2/f/c' }]
    const straddleRows = [[3, 'b2/f/c', 0]]
    const got: Awaited<ReturnType<typeof read>>[] = []
    for (const [asks, points] of [[depths, false], [depths, true], [straddle, false], [straddle, true]] as const) got.push(await read([...asks], points))
    expect(got).toEqual([
      { spans: 3, groups: 3, rows: depthsRows },
      { spans: 1, groups: 3, rows: depthsRows },
      { spans: 1, groups: 2, rows: straddleRows },
      { spans: 1, groups: 1, rows: straddleRows },
    ])
  })

  it('a round of lookups at two depths is one span selection on the scan it reads', async () => {
    // 2026-08-04 → 08-05 at a 7.5 floor, doubling per level: 08-04 draws `b2` (10) and `b1/d` (20), 08-05 neither
    // (0, 10), so both are read on 08-05, in one round: `b2` at depth 1 and `b1/d` at depth 2. Nothing either scan
    // drew is under them, so no other round follows.
    const events: string[] = []
    let walking = false
    const d = await buildDiff(env, {
      from: '2026-08-04', to: '2026-08-05', path: '', w: 1280, h: 800, minArea: 12, atten: 2, top: 100, threshold: 7.5,
      trace: n => {
        if (n === 'rounds') walking = true
        if (walking && (n === 'rounds' || n === 'askspan')) events.push(n)
      },
    })
    expect({ events, lookups: d.lookups, capped: d.lookups_capped }).toEqual({ events: ['rounds', 'askspan'], lookups: 2, capped: false })
  })
})
