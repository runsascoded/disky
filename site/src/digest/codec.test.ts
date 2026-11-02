import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestParams } from './common'
import { decodeParams, encodeParams } from './codec'

describe('digest params in a URL', () => {
  it('round-trips, defaults left out', () => {
    const p: DigestParams = {
      windowDays: 45, todayAt: [0.6, 0.95], diff: 'mtd', headline: ['window'], weeks: 'month', quotas: 'circles', plot: 'pools', alertAt: 85,
    }
    expect([encodeParams(DEFAULT_PARAMS), encodeParams(p), decodeParams(encodeParams(p))]).toEqual(['', 'w45,t0.6-0.95,dm,hw,km,qc,pp,a85', p])
    expect(decodeParams('h')).toEqual({ ...DEFAULT_PARAMS, headline: [] })
  })
  it('drops what it cannot read', () => {
    expect(decodeParams('w0,w999,t0.9-0.5,dx,hmm,kq,zz,qx,pz,a0,a101')).toEqual(DEFAULT_PARAMS)
  })
})
