import { describe, expect, it } from 'vitest'
import { diffCellText } from './DiffTreemap'

const T = 1e12
const fmtBytes = (b: number) => `${(b / T).toFixed(1)} T`
const fmtDelta = (d: number) => (d >= 0 ? '+' : '−') + fmtBytes(Math.abs(d))

describe('diffCellText', () => {
  it('grew and shrank, summing to the net: the table\'s `+grew −shrank = net`, then the net', () => {
    expect(diffCellText({ added: 37.2 * T, removed: 20.4 * T, delta: 16.8 * T }, fmtBytes, fmtDelta))
      .toEqual(['+37.2 T −20.4 T = +16.8 T', '+16.8 T'])
  })

  it('only grew, or only shrank: the net alone', () => {
    expect([
      diffCellText({ added: 23 * T, removed: 0, delta: 23 * T }, fmtBytes, fmtDelta),
      diffCellText({ added: 0, removed: 15 * T, delta: -15 * T }, fmtBytes, fmtDelta),
    ]).toEqual([['+23.0 T'], ['−15.0 T']])
  })

  it('a residue below the printed precision (rows folded under the threshold) still reads true: the sum', () => {
    expect(diffCellText({ added: 37.171 * T, removed: 20.361 * T, delta: 16.816 * T }, fmtBytes, fmtDelta))
      .toEqual(['+37.2 T −20.4 T = +16.8 T', '+16.8 T'])
  })

  it('grew − shrank printing other than the net (rows not covering the cell): the net alone, never a false sum', () => {
    expect(diffCellText({ added: 30 * T, removed: 10 * T, delta: 16.8 * T }, fmtBytes, fmtDelta)).toEqual(['+16.8 T'])
  })
})
