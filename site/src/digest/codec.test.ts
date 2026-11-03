import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestParams } from './common'
import { decodeParams, encodeParams, parseParams } from './codec'

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
  it('parses strictly for the job', () => {
    const p: DigestParams = {
      windowDays: 45, todayAt: [0.6, 0.95], diff: 'mtd', headline: ['window'], weeks: 'month', quotas: 'circles', plot: 'pools', alertAt: 85,
    }
    expect([parseParams(''), parseParams(encodeParams(p)), parseParams('h')]).toEqual([DEFAULT_PARAMS, p, { ...DEFAULT_PARAMS, headline: [] }])
    const bad = ['w0', 'w999', 't0.9-0.5', 'dx', 'hmm', 'kq', 'zz', 'qx', 'pz', 'a0', 'a101', 'w30,', ',w30', 'w30,w45']
    const errs = bad.map(s => {
      try {
        parseParams(s)
        return null
      } catch (e) {
        return (e as Error).message
      }
    })
    expect(errs).toEqual([
      ...bad.slice(0, 11).map(s => `digest params: unreadable field "${s}" in "${s}"`),
      'digest params: unreadable field "" in "w30,"',
      'digest params: unreadable field "" in ",w30"',
      'digest params: repeated field "w" in "w30,w45"',
    ])
  })
})
