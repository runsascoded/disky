import { describe, expect, it } from 'vitest'
import { classSegments, fracLabel } from './classStrip'

const GiB = 1024 ** 3

describe('classSegments', () => {
  it('largest class first, empty classes dropped, shares and list-price $/mo per class and in total', () => {
    expect(classSegments({ 1: 600 * GiB, 3: 300 * GiB, 2: 100 * GiB, 4: 0 })).toEqual({
      segs: [
        { c: '1', name: 'Standard', bytes: 600 * GiB, frac: 0.6, usd: 12 },
        { c: '3', name: 'Coldline', bytes: 300 * GiB, frac: 0.3, usd: 1.2 },
        { c: '2', name: 'Nearline', bytes: 100 * GiB, frac: 0.1, usd: 1 },
      ],
      usd: 14.2,
    })
  })

  it('an unknown class keeps its id and Standard’s price; an empty mix has no segments', () => {
    expect([classSegments({ 9: GiB }), classSegments({})]).toEqual([
      { segs: [{ c: '9', name: '9', bytes: GiB, frac: 1, usd: 0.02 }], usd: 0.02 },
      { segs: [], usd: 0 },
    ])
  })
})

describe('fracLabel', () => {
  it('whole percent, `<1%` for a sliver', () => {
    expect([fracLabel(0.6), fracLabel(0.004), fracLabel(0), fracLabel(1), fracLabel(0.125)]).toEqual(['60%', '<1%', '0%', '100%', '13%'])
  })
})
