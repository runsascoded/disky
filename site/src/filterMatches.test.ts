import { describe, expect, it } from 'vitest'
import { seriesMatches } from './filterMatches'

const m = (i: number) => ({ path: `bkt/m${i}`, bytes: 1000 - i, objects: 1 })

describe('seriesMatches (a filtered subtree response → the series input)', () => {
  it('an uncapped list is every match root, counted', () => {
    expect(seriesMatches({ matched: [m(0), m(1)], matchCount: { count: 2, bytes: 1999, objects: 2 } })).toEqual({ paths: ['bkt/m0', 'bkt/m1'], pathsTotal: 2 })
  })
  it('a capped list is no root list: the series gets the exact count only (and asks with `q`)', () => {
    const matched = Array.from({ length: 205 }, (_, i) => m(i))
    expect(seriesMatches({ matched, matchCount: { count: 21_735, bytes: 7_396_315_609_520, objects: 603_889 }, matchesCapped: true })).toEqual({ paths: undefined, pathsTotal: 21_735 })
  })
  it('no response yet', () => {
    expect(seriesMatches(undefined)).toEqual({ paths: undefined, pathsTotal: undefined })
  })
})
