import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { sqliteD1, type Sqlite, withLedger } from './testD1'
import { type D1Variant, readJson, seedGeneration } from './testStore'
import { buildDiff, type Diff, type DiffRow } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The diff walk's point lookups (`buildDiff`: a name one scan drew, read on the other) over `fixtures/v2-diff/`: two
// store generations of bucket `bk` (`fixtures/gen.py`, MiB), `B` after a deletion.
//
//   A: del/x/p/{1,2} 256 each, del/x/q/1 256, del/y 128 · shrink/{a,b} 100 each · keep/k 300 ·
//      deep/d1/big 400, deep/d1/stay 200 · tiny/t 1
//   B: shrink/a 8 · keep/k 300 · deep/d1/stay 200 · new1 64 · tiny/t 1
//
// Diffed at `bk` with a 16 MiB floor doubling per level (`atten: 2`): `del`'s subtree (8 names) is drawn on A only,
// `shrink` on A only though B still holds it (8 MiB, under the floor), `deep/d1/big` on A only under a directory both
// draw, `new1` on B only; `tiny` is under the floor on both.

const A = '2026-09-28T0001'
const B = '2026-09-29T0001'
const MiB = 1 << 20
let env: Env

const FILES = (scan: string) => ({ path: { parquet: `v2-diff/${scan}/path-index.parquet`, groups: `v2-diff/${scan}/path-index.groups.json` }, bysize: { parquet: `v2-diff/${scan}/path-index-bysize.parquet`, groups: `v2-diff/${scan}/path-index-bysize.groups.json` } })
/** A D1 holding the fixture's two scans as dates `a` and `b`, and what `more` adds. */
const seeded = async (a: string, b: string, more?: (raw: Sqlite) => void): Promise<Env> => {
  const { db, raw } = await sqliteD1()
  for (const [scan, date] of [['a', a], ['b', b]]) seedGeneration(raw, { date, gen: `g${scan}`, dir: `cw-l2/${date}/index/g${scan}`, variants: await readJson<Record<string, D1Variant>>(`v2-diff/${scan}/d1.json`), files: FILES(scan) })
  more?.(raw)
  return { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  env = await seeded(A, B)
})

/** `env`'s D1 answering no metadata (`rg_json`) for the row groups of `date`'s `variant` while `on()`: the span query
 *  before it still returns them, as when a scan's footer rows are deleted between a read's two statements. Patched in
 *  place (an open handle holds the `env` it was opened with); the returned function puts `prepare` back. */
const withoutGroupJson = (env: Env, date: string, variant: string, on: () => boolean): (() => void) => {
  const db = env.DB as unknown as { prepare: (sql: string) => { bind: (...args: unknown[]) => unknown } }
  const prepare = db.prepare
  db.prepare = (sql: string) => {
    const st = prepare(sql)
    if (!sql.startsWith('SELECT rg, rg_json FROM index_row_groups')) return st
    return { bind: (...args: unknown[]) => (on() && args[0] === date && args[1] === variant ? { all: async () => ({ results: [], success: true, meta: {} }) } : st.bind(...args)) }
  }
  return () => { db.prepare = prepare }
}

const row = (path: string, depth: number, kind: DiffRow['kind'], status: DiffRow['status'], a: number, b: number, objects_before: number, objects_after: number, more: Pick<DiffRow, 'expanded' | 'lookup'> = {}): DiffRow =>
  ({ path, depth, kind, status, bytes_before: a * MiB, bytes_after: b * MiB, objects_before, objects_after, ...more })
const x = { expanded: true } as const

/** `del`'s subtree as A drew it, every name absent on B. */
const DEL_BELOW = [
  row('del/x/p/1', 4, 'file', 'removed', 256, 0, 1, 0),
  row('del/x/p/2', 4, 'file', 'removed', 256, 0, 1, 0),
  row('del/x/q/1', 4, 'file', 'removed', 256, 0, 1, 0),
  row('del/y', 2, 'file', 'removed', 128, 0, 1, 0),
]
/** The whole answer A → B: every name either scan drew, with its size on both. */
const WHOLE: Diff = {
  rows: [
    row('deep', 1, 'dir', 'changed', 600, 200, 2, 1, x),
    row('del', 1, 'dir', 'removed', 896, 0, 4, 0, x),
    // B holds `shrink` under the floor: read there (`lookup: 2`), and its names read too.
    row('shrink', 1, 'dir', 'changed', 200, 8, 2, 1, { ...x, lookup: 2 }),
    row('deep/d1', 2, 'dir', 'changed', 600, 200, 2, 1, x),
    row('del/x', 2, 'dir', 'removed', 768, 0, 3, 0, x),
    row('del/x/p', 3, 'dir', 'removed', 512, 0, 2, 0, x),
    row('del/x/q', 3, 'dir', 'removed', 256, 0, 1, 0, x),
    row('deep/d1/big', 3, 'file', 'removed', 400, 0, 1, 0),
    ...DEL_BELOW,
    row('shrink/b', 2, 'file', 'removed', 100, 0, 1, 0),
    row('shrink/a', 2, 'file', 'changed', 100, 8, 1, 1, { lookup: 2 }),
    row('new1', 1, 'file', 'added', 0, 64, 0, 1),
  ],
  bytes_before: 1997 * MiB, bytes_after: 573 * MiB, objects_before: 10, objects_after: 5, threshold: 16 * MiB, tier: 'path',
  expansions: 8, truncated: false,
  // `del`, `shrink`, `new1` and `deep/d1/big` (their parents are on both scans), then `shrink/{a,b}` (B holds
  // `shrink`). Nothing under `del`: B has no row for it, so none for what is under it.
  lookups: 6, lookups_capped: false,
}

/** `d` with its scans swapped (B → A). */
const swapped = (d: Diff): Diff => ({
  ...d,
  rows: d.rows.map(r => ({ ...r, bytes_before: r.bytes_after, bytes_after: r.bytes_before, objects_before: r.objects_after, objects_after: r.objects_before, status: r.status === 'added' ? 'removed' : r.status === 'removed' ? 'added' : r.status, ...(r.lookup ? { lookup: r.lookup === 1 ? 2 : 1 } : {}) } as DiffRow)),
  bytes_before: d.bytes_after, bytes_after: d.bytes_before, objects_before: d.objects_after, objects_after: d.objects_before,
})

const base = { path: 'bk', w: 1280, h: 800, minArea: 12, atten: 2, top: 500, threshold: 16 * MiB }

describe('a diff\'s lookups', () => {
  it('a deleted subtree costs one lookup: what is under it is drawn with its sizes, unread', async () => {
    const rounds: number[] = []
    const d = await buildDiff(env, { ...base, from: A, to: B, trace: (n, v) => { if (n === 'rounds') rounds.push(v) } })
    expect(d).toEqual(WHOLE)
    // Two rounds of reads for four levels of names: the first four lookups at once, whatever their depth, then
    // `shrink`'s two.
    expect(rounds).toEqual([1, 1])
  })

  it('the same the other way round: an added subtree is one lookup on the older scan', async () => {
    expect(await buildDiff(env, { ...base, from: B, to: A })).toEqual(swapped(WHOLE))
  })

  it('a directory under the floor is not absent: its names are read, and drawn with what they hold', async () => {
    const d = await buildDiff(env, { ...base, from: A, to: B })
    expect(d.rows.filter(r => r.path.startsWith('shrink'))).toEqual([
      row('shrink', 1, 'dir', 'changed', 200, 8, 2, 1, { ...x, lookup: 2 }),
      row('shrink/b', 2, 'file', 'removed', 100, 0, 1, 0),
      row('shrink/a', 2, 'file', 'changed', 100, 8, 1, 1, { lookup: 2 }),
    ])
  })

  it('a budget short of the names is spent on the heaviest, whatever their depth', async () => {
    // Two lookups: `del` (896 MiB, depth 1) and `deep/d1/big` (400, depth 3) before `shrink` (200) and `new1` (64),
    // which stay in `bk`'s `(other)` on both scans (201 → 73 MiB with `tiny`).
    expect(await buildDiff(env, { ...base, from: A, to: B, lookupCap: 2 })).toEqual({
      ...WHOLE,
      rows: [
        row('deep', 1, 'dir', 'changed', 600, 200, 2, 1, x),
        row('del', 1, 'dir', 'removed', 896, 0, 4, 0, x),
        row('deep/d1', 2, 'dir', 'changed', 600, 200, 2, 1, x),
        row('del/x', 2, 'dir', 'removed', 768, 0, 3, 0, x),
        row('del/x/p', 3, 'dir', 'removed', 512, 0, 2, 0, x),
        row('del/x/q', 3, 'dir', 'removed', 256, 0, 1, 0, x),
        row('deep/d1/big', 3, 'file', 'removed', 400, 0, 1, 0),
        ...DEL_BELOW,
        row('(other)', 1, 'dir', 'changed', 201, 73, 3, 3),
      ],
      expansions: 7, lookups: 2, lookups_capped: true,
    })
  })

  it('a row group whose metadata is missing fails the diff: no name is answered absent by a group that was not read', async () => {
    // B's `path` sort loses its group's metadata once the walk starts (the span query still finds the group): the
    // lookups' reads decoded nothing. Taken as "no row", every name A drew was absent on B, and `shrink` (8 MiB there)
    // with what is under it read as removed.
    let walking = false
    const restore = withoutGroupJson(env, B, 'path', () => walking)
    const got = await buildDiff(env, { ...base, from: A, to: B, trace: n => { if (n === 'rounds') walking = true } })
      .then(d => d.rows.filter(r => r.path.startsWith('shrink')), err => `${(err as Error).name}: ${(err as Error).message}`)
      .finally(restore)
    expect(got).toEqual(`GroupUnread: row group 0 of ${B}'s \`path\` sort has no metadata: its rows were not read`)
  })

  it('a budget of exactly the names read is the whole answer', async () => {
    // Six: the level-by-level walk spent them on `del`'s subtree (13 names one scan drew) and folded the rest.
    expect(await buildDiff(env, { ...base, from: A, to: B, lookupCap: 6 })).toEqual(WHOLE)
    expect(await buildDiff(env, { ...base, from: A, to: B, lookupCap: 1e9 })).toEqual(WHOLE)
  })
})

// Under a user's lens a lookup reads that user's rows (here the `path` sort filtered to her: the fixture attributes
// nothing to anyone), so "no row" is her absence at the path, not the path's: what the ledger assigns her under it is
// still hers. Carol is assigned `keep/` and the object `shrink/a`: her view draws `shrink` and `shrink/a` on A (100
// MiB), and on B (8 MiB) folds them under the floor.
describe('a lens diff\'s lookups', () => {
  const LA = '2026-10-01T0001'
  const LB = '2026-10-02T0001'
  const lens = { key: 'carol' }
  let lensEnv: Env
  beforeAll(async () => {
    lensEnv = await seeded(LA, LB, raw => {
      withLedger(raw)
      const assigned: [string, string][] = [['s3://bk/keep/', 'NULL'], ['s3://bk/shrink/a', "'object'"]]
      raw.exec(assigned.map(([prefix, kind], i) =>
        `INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner, kind) VALUES (${i + 1}, 'ann', 1, '${LA}', '${prefix}', 1, 'carol', ${kind});` +
        `INSERT INTO owner_prefixes (action_id, prefix, owner, ts, kind) VALUES (${i + 1}, '${prefix}', 'carol', 1, ${kind});`).join('\n'))
    })
  })

  it('a path the lens holds no row of is not absent: an assignment under it is read, and drawn with its size', async () => {
    const inferred: number[] = []
    const d = await buildDiff(lensEnv, { ...base, from: LA, to: LB, lens, trace: (n, v) => { if (n === 'inferred') inferred.push(v) } })
    // B's lookup of `shrink` finds no row of carol's, and its total isn't read (no assignment covers `shrink`): that
    // is not `shrink`'s absence, so `shrink/a` is read there — its total, as her assignment covers it. Taken as
    // absence, `shrink/a` was inferred removed (100 → 0 MiB, one lookup) where she holds 8 MiB.
    // (Only `shrink/a`'s row: `shrink`'s own is that lookup's answer, as before this walk.)
    expect({ rows: d.rows.filter(r => r.path === 'shrink/a'), lookups: d.lookups, inferred }).toEqual({
      rows: [row('shrink/a', 2, 'file', 'changed', 100, 8, 1, 1, { lookup: 2 })], lookups: 2, inferred: [],
    })
  })

  it('a row group whose metadata is missing fails a lens diff too (its lookups read per name)', async () => {
    let walking = false
    const restore = withoutGroupJson(lensEnv, LB, 'path', () => walking)
    const got = await buildDiff(lensEnv, { ...base, from: LA, to: LB, lens, trace: n => { if (n === 'rounds') walking = true } })
      .then(d => d.rows.filter(r => r.path === 'shrink/a'), err => `${(err as Error).name}: ${(err as Error).message}`)
      .finally(restore)
    expect(got).toEqual(`GroupUnread: row group 0 of ${LB}'s \`path\` sort has no metadata: its rows were not read`)
  })
})
