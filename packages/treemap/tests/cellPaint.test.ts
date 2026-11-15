import { describe, expect, it } from 'vitest'
import { bandRects, stripeBackground } from '../src/cellPaint'

describe('shared cell paint', () => {
  it('stacks an unchanged region above its change band without painting twice', () => {
    expect(bandRects([{ color: 'gray', frac: .8 }, { color: 'green', frac: .2 }], 200)).toEqual([
      { color: 'gray', top: 0, height: 160 }, { color: 'green', top: 160, height: 40 },
    ])
    expect(bandRects(undefined, 200)).toEqual([])
    expect(bandRects([{ color: 'gray', frac: 0 }], 200)).toEqual([{ color: 'gray', top: 0, height: 0 }])
  })
  it('gives DOM hatches the same stripe parameters as the canvas painter', () => {
    expect(stripeBackground([{ angle: 135, color: 'rgb(0 0 0 / 0.3)', width: 1.5, period: 7 }])).toBe('repeating-linear-gradient(135deg, rgb(0 0 0 / 0.3) 0 1.5px, transparent 1.5px 7px)')
    expect(stripeBackground([])).toBeUndefined()
  })
})
