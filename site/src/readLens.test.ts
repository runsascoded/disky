import { describe, expect, it } from 'vitest'
import { readLogged, readState, unloggedOf } from './readLens'

const access = { from: 100, to: 110, buckets: ['jc-taxes'] }

describe('readLogged', () => {
  it('covers only the listed buckets; a folded cell or a missing segment is uncovered', () => {
    const logged = readLogged(access)
    expect(['jc-taxes', 'ctbk', 'crashes', '(+2)', undefined].map(logged)).toEqual([true, false, false, false, false])
  })

  it('covers everything when the scan predates `buckets`, and when there is no access window', () => {
    expect([readLogged({ from: 100, to: 110 })('ctbk'), readLogged(undefined)('ctbk')]).toEqual([true, true])
  })
})

describe('readState', () => {
  const range = { min: 100, max: 110 }
  it('places a read on the window, linearly without an axis, by the axis when given', () => {
    expect([
      readState(105, true, range),
      readState(110, false, range),
      readState(105, true, { ...range, t: d => (d - 100) / 20 }),
    ]).toEqual([{ kind: 'read', t: 0.5 }, { kind: 'read', t: 1 }, { kind: 'read', t: 0.25 }])
  })

  it('puts a read on a one-day window at the new end (not "no observed reads")', () => {
    expect(readState(110, true, { min: 110, max: 110, t: () => NaN })).toEqual({ kind: 'read', t: 1 })
  })

  it('tells a logged cell with no read from an unlogged one', () => {
    expect([readState(null, true, range), readState(undefined, false, range), readState(null, false, null)])
      .toEqual([{ kind: 'never' }, { kind: 'unlogged' }, { kind: 'unlogged' }])
  })
})

describe('unloggedOf', () => {
  it('lists the uncovered top-level names in order, folded cells left out', () => {
    expect(unloggedOf(['crashes', 'jc-taxes', 'ctbk', '(+3)'], readLogged(access))).toEqual(['crashes', 'ctbk'])
  })
})
