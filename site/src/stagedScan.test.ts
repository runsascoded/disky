import { describe, expect, it } from 'vitest'
import { pickedScan, pickedSlug, type ScanDeletion, sizedScan, withoutDeleted } from './stagedScan'

const OLD = '2026-11-04T0703', NEW = '2026-11-05T0702', NEWER = '2026-11-06T0701'

describe('the picked scan in the URL (`?d=`)', () => {
  it('writes a picked scan as its compact id, the newest as nothing', () => {
    expect([pickedSlug(OLD, [NEWER, NEW, OLD]), pickedSlug(NEWER, [NEWER, NEW, OLD]), pickedSlug('2026-11-03', [NEW, '2026-11-03'])]).toEqual(['2611040703', undefined, '261103'])
  })
  it('reads it back: the scan spelled in full, a day as its latest, anything else as no pick', () => {
    const scans = [NEWER, NEW, '2026-11-05T0101', OLD]
    expect([
      pickedScan('2611040703', scans), pickedScan('2611050101', scans), pickedScan('261105', scans), pickedScan('2026-11-04T0703', scans),
      pickedScan('261101', scans), pickedScan('nope', scans), pickedScan(undefined, scans), pickedScan('2611040703', []),
    ]).toEqual([OLD, '2026-11-05T0101', NEW, OLD, null, null, null, null])
  })
  it('round-trips every scan but the newest', () => {
    const scans = [NEWER, NEW, OLD]
    expect(scans.map(s => sizedScan(scans, pickedScan(pickedSlug(s, scans), scans)))).toEqual(scans)
  })
})

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
  it('part of it deleted (overwrites kept, a drifted folder skipped): the exact remainder, its owners no longer known', () => {
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
  describe('a run that did not end with a result arrives with no amounts (`scanDeletions`)', () => {
    const unknown = band({ bytes: 0, objects: 0 })
    it('alone (killed, a failed directory): the item stays, whole', () => {
      expect(withoutDeleted({ [P]: stat }, [unknown])).toEqual({ [P]: stat })
    })
    it('beside a run that deleted part: what the first deleted is not known, so no remainder is stated', () => {
      expect(withoutDeleted({ [P]: stat }, [unknown, band({ bytes: 700, objects: 7 })])).toEqual({ [P]: stat })
    })
    it('beside a run that deleted every object: nothing left', () => {
      expect(withoutDeleted({ [P]: stat }, [unknown, band({})])).toEqual({})
    })
  })
  it('a band with no row at this scan adds nothing', () => {
    expect(withoutDeleted({ [Q]: stat }, [band({})])).toEqual({ [Q]: stat })
  })
})
