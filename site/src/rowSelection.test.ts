import { describe, expect, it } from 'vitest'
import { findConflicts } from 'use-kbd'
import { countMoves } from './rowSelection'

type Move = [number, boolean]
const recorder = () => {
  const calls: Move[] = []
  return { calls, sel: { moveCursor: (target: unknown, extend = false) => { calls.push([target as number, extend]) } } }
}

describe('countMoves', () => {
  it("registers use-kbd's count-prefixed moves under their ids, scoped to the table's focus mode", () => {
    const moves = countMoves('tbl', 'Children table', 'tbl:focused', recorder().sel)
    expect(Object.entries(moves).map(([id, a]) => [id, a.label, a.group, a.mode, a.defaultBindings])).toEqual([
      ['tbl:up-n', 'Up N rows', 'Children table', 'tbl:focused', ['\\d+ k', '\\d+ arrowup']],
      ['tbl:down-n', 'Down N rows', 'Children table', 'tbl:focused', ['\\d+ j', '\\d+ arrowdown']],
      ['tbl:extend-up-n', 'Extend up N rows', 'Children table', 'tbl:focused', ['\\d+ shift+k', '\\d+ shift+arrowup']],
      ['tbl:extend-down-n', 'Extend down N rows', 'Children table', 'tbl:focused', ['\\d+ shift+j', '\\d+ shift+arrowdown']],
    ])
  })

  it('moves the cursor by the typed count (1 without one), extending for the shifted pair', () => {
    const { calls, sel } = recorder()
    const moves = countMoves('tbl', 'Children table', 'tbl:focused', sel)
    for (const id of ['tbl:up-n', 'tbl:down-n', 'tbl:extend-up-n', 'tbl:extend-down-n']) moves[id].handler!(undefined, [3])
    moves['tbl:down-n'].handler!(undefined, undefined)
    expect(calls).toEqual([[-3, false], [3, false], [-3, true], [3, true], [1, false]])
  })

  it("doesn't conflict with the Map's digit keys once scoped (it does as a global)", () => {
    const moves = countMoves('tbl', 'Children table', 'tbl:focused', recorder().sel)
    const globals: Record<string, string[]> = {
      'mode:size': ['1'], 'mode:user': ['2'], 'mode:age': ['3'],
      'tbl:up': ['k', 'arrowup'], 'tbl:down': ['j', 'arrowdown'], 'tbl:extend-up': ['shift+k', 'shift+arrowup'], 'tbl:extend-down': ['shift+j', 'shift+arrowdown'],
    }
    const keymap: Record<string, string[]> = {}
    for (const [id, keys] of [...Object.entries(globals), ...Object.entries(moves).map(([id, a]) => [id, a.defaultBindings!] as const)]) {
      for (const k of keys) (keymap[k] ??= []).push(id)
    }
    expect(findConflicts(keymap, id => moves[id]?.mode)).toEqual(new Map())
    expect([...findConflicts(keymap, () => undefined).keys()].sort()).toEqual([
      '1', '2', '3',
      '\\d+ arrowdown', '\\d+ arrowup', '\\d+ j', '\\d+ k',
      '\\d+ shift+arrowdown', '\\d+ shift+arrowup', '\\d+ shift+j', '\\d+ shift+k',
    ])
  })
})
