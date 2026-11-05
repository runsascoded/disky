import { describe, expect, it } from 'vitest'
import { type ScanDeletion, sizedScan, withoutDeleted } from './stagedScan'

const OLD = '2026-11-04T0703', NEW = '2026-11-05T0702', NEWER = '2026-11-06T0701'

describe('sizedScan: which scan a long-open /staged sizes at', () => {
  it('follows the newest when nothing was picked', () => {
    expect([sizedScan([NEW, OLD], null), sizedScan([NEWER, NEW, OLD], null)]).toEqual([NEW, NEWER])
  })
  it('keeps a picked scan as newer ones land', () => {
    expect(sizedScan([NEWER, NEW, OLD], OLD)).toBe(OLD)
  })
  it('a picked scan the list dropped falls back to the newest', () => {
    expect(sizedScan([NEWER, NEW], OLD)).toBe(NEWER)
  })
  it('no scans yet: none', () => {
    expect(sizedScan([], null)).toBe('')
  })
})

describe('withoutDeleted: a scan\'s sizes less what a real run on that same scan has since deleted', () => {
  const P = 'gs://b/x/', Q = 'gs://b/y/'
  const stat = { b: 1000, o: 10, d: 5, us: [['ann', 1000]] as [string, number][], cb: { 2: 100 } }
  const band = (over: Partial<ScanDeletion>): ScanDeletion => ({ prefix: P, bytes: 1000, objects: 10, gone: 0, ...over })

  it('all of it deleted: nothing left, as if empty at the scan; others untouched', () => {
    expect(withoutDeleted({ [P]: stat, [Q]: stat }, [band({})])).toEqual({ [Q]: stat })
  })
  it('all of it deleted or already gone: nothing left', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 700, objects: 7, gone: 3 })])).toEqual({})
  })
  it('a resumed run, whose band counts the first run\'s deletions as gone: nothing left', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 400, objects: 4 }), band({ bytes: 600, objects: 6, gone: 4 })])).toEqual({})
  })
  it('part of it deleted (a stopped run, overwrites kept): the exact remainder, its owners no longer known', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 700, objects: 7 })])).toEqual({ [P]: { b: 300, o: 3, d: 5 } })
  })
  it('part deleted with some gone: gone objects have no recorded bytes, so the remainder can\'t be stated: left whole', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 500, objects: 5, gone: 2 })])).toEqual({ [P]: stat })
  })
  it('two partial runs, which may overlap: left whole', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 100, objects: 1 }), band({ bytes: 100, objects: 1 })])).toEqual({ [P]: stat })
  })
  it('a band that deleted nothing changes nothing', () => {
    expect(withoutDeleted({ [P]: stat }, [band({ bytes: 0, objects: 0 })])).toEqual({ [P]: stat })
  })
  it('a band with no row at this scan adds nothing', () => {
    expect(withoutDeleted({ [Q]: stat }, [band({})])).toEqual({ [Q]: stat })
  })
})
