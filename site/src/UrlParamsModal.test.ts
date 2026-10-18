import { describe, expect, it } from 'vitest'
import type { ParamReflection } from 'use-prms'
import { groupParams, withCode, withoutKeys } from './UrlParamsModal'

const p = (key: string, refs: number, raw?: string): ParamReflection =>
  ({ key, keys: [key], strategy: 'query', refs, raw, state: raw === undefined ? 'absent' : 'canonical', multi: false, defaultEncoded: undefined, value: raw, param: { encode: v => v as string, decode: v => v } } as ParamReflection)

describe('groupParams', () => {
  it('splits set, read here, and read elsewhere, each by key; the unrecognized beside', () => {
    const g = groupParams([p('ps', 0, 'scan'), p('c', 1), p('d', 2, '260918'), p('sort', 0), p('ac', 1)], [{ key: 'zz', raw: '1' }, { key: 'aa', raw: '' }])
    expect({ set: g.set.map(x => x.key), here: g.here.map(x => x.key), elsewhere: g.elsewhere.map(x => x.key), unknown: g.unknown.map(x => x.key) })
      .toEqual({ set: ['d', 'ps'], here: ['ac', 'c'], elsewhere: ['sort'], unknown: ['aa', 'zz'] })
  })
  it('leaves out an absent hidden key, never a set one', () => {
    const g = groupParams([p('date', 0), p('from', 0, '2610'), p('l', 1)], [], k => ['date', 'from', 'l'].includes(k))
    expect([g.set, g.here, g.elsewhere].map(ps => ps.map(x => x.key))).toEqual([['from'], [], []])
  })
})

describe('withoutKeys', () => {
  it('drops the keys, keeping the rest in order', () => {
    expect([withoutKeys('?d=1&ps=scan&c=t', ['ps']), withoutKeys('?ps=scan', ['ps']), withoutKeys('?a=1&b=2', ['a', 'b', 'x'])])
      .toEqual(['?d=1&c=t', '', ''])
  })
})

describe('withCode', () => {
  it('splits out the backticked spans', () => {
    expect(withCode('`iv` reads the store; `scan` per-scan')).toEqual([{ code: 'iv' }, ' reads the store; ', { code: 'scan' }, ' per-scan'])
  })
})
