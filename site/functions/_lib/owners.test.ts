/** `ownerLens` as a spec: the assignments fold behind a user lens — bands under
 * the newest covering assignment, U's slice elsewhere (owners.ts header). */
import { describe, expect, it } from 'vitest'
import type { AssignmentRow } from './ownerBands.js'
import { ownerLens, poolLens } from './owners.js'

const assignment = (prefix: string, owner: string | null, ts: number, bytes: number, us: Record<string, number>, objects = 1, uo: Record<string, number> = {}): AssignmentRow =>
  ({ prefix, owner, ts, action_id: ts, bytes, objects, us, uo })

// U = 'u'. A (v's) holds B (u's, newer, nested); C (u's) holds D (v's, newer,
// nested); R is a release; E (u's) holds G (w's, OLDER, so E repaints it).
const CLAIMS: AssignmentRow[] = [
  assignment('gs://b/a/', 'v', 10, 100, { u: 30, v: 50 }),
  assignment('gs://b/a/x/', 'u', 20, 40, { u: 5, v: 35 }),
  assignment('gs://b/c/', 'u', 5, 200, { u: 120, w: 80 }),
  assignment('gs://b/c/d/', 'v', 6, 50, { u: 10 }),
  assignment('gs://b/r/', null, 7, 10, { u: 4 }),
  assignment('gs://b/e/', 'u', 1, 60, { u: 20 }),
  assignment('gs://b/e/g/', 'w', 0, 30, { u: 9 }),
]

describe('ownerLens', () => {
  const ol = ownerLens(CLAIMS, 'u')!

  it('is null with no assignments (the lens is plain attribution)', () => {
    expect(ownerLens([], 'u')).toBeNull()
  })

  it('needs a total exactly where U’s assignment covers the path', () => {
    expect(['b', 'b/a', 'b/a/x', 'b/c', 'b/c/d', 'b/c/e', 'b/e/g', 'b/z'].map(p => ol.needsTotal(p)))
      .toEqual([false, false, true, true, false, true, true, false])
  })

  it('names the outermost U-assigned regions the scan attributes to others', () => {
    // B (inside v's A), C (holding v's D), E (holding repainted G): all three
    // are U's outermost assignments; G is inside E.
    expect(ol.regions('b')).toEqual([{ path: 'b/a/x', depth: 3, all: 40, objects: 1 }, { path: 'b/c', depth: 2, all: 200, objects: 1 }, { path: 'b/e', depth: 2, all: 60, objects: 1 }])
    expect(ol.regions('b/a')).toEqual([{ path: 'b/a/x', depth: 3, all: 40, objects: 1 }])
    expect(ol.regions('b/c')).toEqual([])
  })

  it('needs no total under an assignment the scan already attributes wholly to U', () => {
    const full = ownerLens([assignment('gs://b/f/', 'u', 1, 50, { u: 50 })], 'u')!
    expect(full.regions('b')).toEqual([])
    expect(full.needsTotal('b/f/deep')).toBe(false)
    expect(full.value('b/f/deep', null, 12)).toBe(12) // U's slice is the total there
  })

  it('values an unread assignment path from the manifest total', () => {
    expect(ol.value('b/c', null, null)).toBe(150)
    expect(ol.value('b/a/x', null, null)).toBe(40)
  })

  it('values an unread ancestor as its bands alone (a lower bound)', () => {
    expect(ol.value('b', 1000, null)).toBe(40 + 150 + 4 + 60)
  })

  it('values a path as its residual under its cover plus U’s bands below', () => {
    // root: attributed outside every top assignment (300 − 30 − 120 − 4 − 20 = 126)
    // + B whole (40) + C minus D (150) + R's u-slice (4) + E incl. repainted G (60)
    expect(ol.value('b', 1000, 300)).toBe(380)
    expect(ol.value('b/a', 100, 30)).toBe(40) // v's band contributes nothing; B inside is u's
    expect(ol.value('b/a/x', 40, 5)).toBe(40)
    expect(ol.value('b/c', 200, 120)).toBe(150)
    expect(ol.value('b/c/d', 50, 10)).toBe(0)
    expect(ol.value('b/c/e', 20, 3)).toBe(20) // an unowned dir inside C: all of it is u's
    expect(ol.value('b/r', 10, 4)).toBe(4) // a release: back to attribution
    expect(ol.value('b/e', 60, 20)).toBe(60) // G is older than E, so E's assignment repaints it
    expect(ol.value('b/e/g', 30, 9)).toBe(30)
    expect(ol.value('b/z', null, 7)).toBe(7) // nothing assigned here: attribution, no total needed
  })

  it('refuses to guess a total where an assigned cover needs it', () => {
    expect(() => ol.value('b/c/e', null, 3)).toThrow(/total bytes needed at b\/c\/e/)
  })

  it('canonicalizes assignees and slice keys', () => {
    const o = ownerLens([assignment('gs://b/q/', 'U@example.com', 1, 50, { 'u@example.com': 12 })], 'u')!
    expect(o.value('b/q', 50, 12)).toBe(50)
    expect(o.value('b', 500, 100)).toBe(500 - 500 + 100 - 12 + 50) // = 138
  })
})

describe('poolLens', () => {
  // The same ledger. Effective assignees: A v, B u, C u, D v (newer than C), R
  // released, E u, G u (E repaints it). Scan-unowned share per assignment
  // (bytes − Σ us): A 20, B 0, C 0, D 40, R 6, E 40, G 21.

  it('is null with no assignments (the pool is plain attribution)', () => {
    expect(poolLens([], 'unowned')).toBeNull()
  })

  it('unowned: the scan’s unowned bytes outside every assignment, plus a release’s', () => {
    const pl = poolLens(CLAIMS, 'unowned')!
    // root: 400 scan-unowned − top A/C/R/E's (20 + 0 + 6 + 40) = 334, + R's 6
    expect(pl.value('b', 1000, 400)).toBe(340)
    expect(pl.value('b/c/d/z', 10, 5)).toBe(0) // under v's D: owned, whatever the scan said
    expect(pl.value('b/r/q', 5, 3)).toBe(3) // under a release: the scan's attribution
    expect(pl.value('b/z', 7, 7)).toBe(7) // untouched by the ledger
    expect(['b', 'b/a', 'b/c/d'].map(p => pl.needsTotal(p))).toEqual([false, false, false])
    expect(pl.regions('b')).toEqual([])
  })

  it('owned: every assigned band whole, the scan’s owned bytes elsewhere — the complement of unowned', () => {
    const pl = poolLens(CLAIMS, 'owned')!
    // root: 600 scan-owned − top A/C/R/E's (80 + 200 + 4 + 20) = 296, + bands
    // A−B 60, B 40, C−D 150, D 50, E−G 30, G 30, R's owned slice 4
    expect(pl.value('b', 1000, 600)).toBe(660)
    expect(pl.value('b', 1000, 600) + poolLens(CLAIMS, 'unowned')!.value('b', 1000, 400)).toBe(1000)
    expect(pl.value('b/c/d/z', 10, 5)).toBe(10)
  })

  it('owned except u: owned minus u’s lens', () => {
    const pl = poolLens(CLAIMS, { not: ['u'] })!
    // root: 250 scan-owned-by-others − top A/C's (50 + 80) = 120, + A−B 60, D 50
    expect(pl.value('b', 1000, 250)).toBe(230)
    expect(pl.value('b', 1000, 250) + ownerLens(CLAIMS, 'u')!.value('b', 1000, 350)).toBe(660)
    expect(pl.value('b/a/x/y', 9, 0)).toBe(0) // u's B
  })

  it('objects fold exactly from the manifest’s per-user counts (`uo`), so owned + unowned objects are the total', () => {
    // A (v's) holds 10 objects, 3 of them u's and 4 v's (3 unowned); B (u's, inside A) 2, 1 v's.
    const rows = [
      assignment('gs://b/a/', 'v', 10, 100, { u: 30, v: 50 }, 10, { u: 3, v: 4 }),
      assignment('gs://b/a/x/', 'u', 20, 40, { u: 5, v: 35 }, 2, { v: 1 }),
    ]
    const [un, ow] = (['unowned', 'owned'] as const).map(p => poolLens(rows, p)!.o)
    // root: 50 objects, 20 scan-unowned. A's 3 unowned leave; B's own unowned
    // object went with A, nothing comes back (B is u's).
    expect([un.value('b', 50, 20), ow.value('b', 50, 30)]).toEqual([17, 33])
    // u's lens: residual u objects outside A (30 − 3 − 0 … A is v's, so its 3 u objects leave) + B whole
    expect(ownerLens(rows, 'u')!.o.value('b', 50, 12)).toBe(12 - 3 + 2)
  })
})
