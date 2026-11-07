import { describe, expect, it } from 'vitest'
import { candidates, fitCandidate, inlineRoom, NAME_MIN_PX } from '../src/cellText'

const px1 = (s: string) => s.length // 1 px per character

describe('cellText', () => {
  it('candidates: a string is its own one candidate', () => {
    expect([candidates('+1 T'), candidates(['a', 'b'])]).toEqual([['+1 T'], ['a', 'b']])
  })

  it('fitCandidate: the first that fits, else the last', () => {
    const cs = ['+37.2 T −20.4 T = +16.8 T', '+16.8 T']
    expect([25, 24, 7, 3].map(room => fitCandidate(cs, room, px1))).toEqual([
      '+37.2 T −20.4 T = +16.8 T',
      '+16.8 T',
      '+16.8 T',
      '+16.8 T',
    ])
  })

  it('inlineRoom: the title bar less padding, gap and the name, the name counted to at most NAME_MIN_PX', () => {
    expect([inlineRoom(300, 4, 6, 20), inlineRoom(300, 4, 6, 200)]).toEqual([300 - 8 - 6 - 20, 300 - 8 - 6 - NAME_MIN_PX])
  })
})
