import { describe, expect, it } from 'vitest'
import { decodeBin, encodeBin } from './groupBin.js'
import { groupDoc, groupRows } from './index.js'

const back = (d: Parameters<typeof encodeBin>[0]) => {
  const b = encodeBin(d)
  // Decoded from its own buffer, as a colo copy is (`coloGroup`): offsets from 0.
  return decodeBin(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer)
}
const plain = (d: { cols: string[]; v: ArrayLike<unknown>[] }) => ({ cols: d.cols, v: d.v.map(c => Array.from(c)) })

describe('groupBin', () => {
  it('decodes every column a group doc holds back exactly', () => {
    const raw = [
      { depth: 2n, path: 'b1/x', size: 5, us: null, mtime: undefined, wts: 1.25, dir: true, mix: 'a', bad: 'b1/\ud800y' },
      { depth: 3n, path: 'b1/é/日本', size: 0, us: 'alice', mtime: 7n, wts: -0.5, dir: false, mix: 3, bad: 'ok' },
      { depth: 4n, path: '', size: 2 ** 53 - 1, us: '', mtime: 9n, wts: 0, dir: null, mix: null, bad: null },
    ]
    const cols = ['depth', 'path', 'size', 'us', 'mtime', 'wts', 'dir', 'mix', 'bad']
    const d = groupDoc(raw, cols)!
    expect(plain(back(d))).toEqual({
      cols,
      v: [
        [2, 3, 4],
        ['b1/x', 'b1/é/日本', ''],
        [5, 0, 2 ** 53 - 1],
        [null, 'alice', ''],
        [null, 7, 9],
        [1.25, -0.5, 0],
        [true, false, null],
        ['a', 3, null],
        ['b1/\ud800y', 'ok', null],
      ],
    })
    expect(groupRows(back(d))).toEqual(groupRows(d))
  })

  it('holds numbers as typed-array views and strings as strings', () => {
    const d = back({ cols: ['n', 's', 'j'], v: [[1, 2.5], ['x', null], [true, 1]] })
    expect([d.v[0] instanceof Float64Array, Array.isArray(d.v[1]), Array.isArray(d.v[2])]).toEqual([true, true, true])
  })

  it('round-trips an empty group', () => {
    expect(plain(back({ cols: ['depth', 'path'], v: [[], []] }))).toEqual({ cols: ['depth', 'path'], v: [[], []] })
  })
})
