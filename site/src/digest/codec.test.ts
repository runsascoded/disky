import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS } from './common'
import { decodeParams, encodeParams } from './codec'

describe('digest params in a URL', () => {
  it('round-trips, defaults left out', () => {
    const p = { windowDays: 45, todayAt: [0.6, 0.95] as [number, number], diff: 'mtd' as const, headline: ['window' as const], weeks: 'month' as const }
    expect([encodeParams(DEFAULT_PARAMS), encodeParams(p), decodeParams(encodeParams(p))]).toEqual(['', 'w45,t0.6-0.95,dm,hw,km', p])
    expect(decodeParams('h')).toEqual({ ...DEFAULT_PARAMS, headline: [] })
  })
  it('drops what it cannot read', () => {
    expect(decodeParams('w0,w999,t0.9-0.5,dx,hmm,kq,zz')).toEqual(DEFAULT_PARAMS)
  })
})
