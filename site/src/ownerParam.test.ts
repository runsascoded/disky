import { describe, expect, it } from 'vitest'
import { bareEmpty, legacyOwner, ownerParam } from './ownerParam'

describe('ownerParam: `?o` golfed', () => {
  it('pools encode short, people as written', () => {
    expect(['unowned', 'owned', 'me', 'rw', '!rw,dlwh', undefined].map(ownerParam.encode)).toEqual(['', '*', 'me', 'rw', '!rw,dlwh', undefined])
  })
  it('decodes the short, long and retired spellings', () => {
    expect(['', '*', 'unowned', 'owned', 'unclaimed', 'claimed', 'rw', undefined].map(ownerParam.decode))
      .toEqual(['unowned', 'owned', 'unowned', 'owned', 'unowned', 'owned', 'rw', undefined])
  })
  it('legacyOwner rewrites only non-canonical pools', () => {
    expect([null, '', '*', 'unowned', 'unclaimed', 'owned', 'claimed', 'rw'].map(legacyOwner))
      .toEqual([null, null, null, '', '', '*', '*', null])
  })
  it('bareEmpty writes `o=` as `o`, leaving other keys alone', () => {
    expect([bareEmpty('o=&d=2026-10-07', 'o'), bareEmpty('fo=&o=*', 'o'), bareEmpty('o=', 'o')]).toEqual(['o&d=2026-10-07', 'fo=&o=*', 'o'])
  })
})
