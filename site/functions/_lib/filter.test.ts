import { describe, expect, it } from 'vitest'
import { filterThreshold, looseThreshold, matchRoots, parsePaths, pickTier, rebasedThreshold, rootRects } from './filter'
import { parseQuery } from './scope'

const ttl = parseQuery('ttl')!
const paths = ['marin', 'tmp', 'tmp/ttl=14d', 'tmp/ttl=14d/run-a', 'tmp/ttl=14d/run-a/ckpt', 'tmp/ttl=7d', 'tmp/scratch', 'iris', 'iris/ttl-misc', 'iris/ttl-misc/inner-ttl']

describe('matchRoots', () => {
  it('keeps the outermost match on each branch; nested matches collapse to the outer one', () => {
    expect(matchRoots(paths, ttl, '')).toEqual(['iris/ttl-misc', 'tmp/ttl=14d', 'tmp/ttl=7d'])
  })
  it('is scoped to the drilled root: ancestors above it do not count', () => {
    expect(matchRoots(paths, ttl, 'tmp')).toEqual(['tmp/ttl=14d', 'tmp/ttl=7d'])
    expect(matchRoots(paths, ttl, 'iris/ttl-misc')).toEqual(['iris/ttl-misc'])
  })
  it('a matching root selects the whole view', () => {
    expect(matchRoots(paths, parseQuery('tmp')!, 'tmp')).toEqual(['tmp'])
  })
  it('case-insensitive whole-path substring, as the page filter has always been', () => {
    expect(matchRoots(['A/TTL', 'b/Ttl=1'], ttl, '')).toEqual(['A/TTL', 'b/Ttl=1'])
    expect(matchRoots(paths, parseQuery('nothing')!, '')).toEqual([])
  })
})

describe('thresholds', () => {
  it('one budget for the forest: matched bytes × min cell area over the canvas', () => {
    expect(filterThreshold(1024 * 1024, 1024, 512, 12)).toBe(24)
  })
  it('re-bases the attenuation on the root depth', () => {
    const thr = rebasedThreshold(100, 2, 2)
    expect([thr(1), thr(2), thr(3), thr(4), thr(5)]).toEqual([100, 100, 100, 200, 400])
  })
  it('the loose threshold is the deepest root’s (the most permissive)', () => {
    const loose = looseThreshold(100, 2, [1, 3])
    expect([loose(2), loose(4), loose(5)]).toEqual([100, 100, 200])
  })
  it('pickTier: the coarsest tier the threshold can’t see below, else fine', () => {
    const tiers = [{ name: 'coarse24', floor: 64 }, { name: 'coarse16', floor: 16384 }, { name: 'coarse20', floor: 1024 }]
    expect(pickTier(tiers, 20000)).toEqual({ name: 'coarse16', floor: 16384 })
    expect(pickTier(tiers, 1024)).toEqual({ name: 'coarse20', floor: 1024 })
    expect(pickTier(tiers, 10)).toBe('fine')
  })
})

describe('rootRects / parsePaths', () => {
  it('one subtree rectangle per root', () => {
    expect(rootRects([{ path: 'tmp/ttl=14d', depth: 2 }, { path: 'iris', depth: 1 }])).toEqual([
      { dLo: 3, dHi: 1e9, pLo: 'tmp/ttl=14d/', pHi: 'tmp/ttl=14d0' },
      { dLo: 2, dHi: 1e9, pLo: 'iris/', pHi: 'iris0' },
    ])
  })
  it('parsePaths: comma or repeated, trimmed, deduped, trailing slashes dropped', () => {
    expect(parsePaths(['tmp/ttl=14d/,iris', ' tmp/ttl=7d ', 'iris/'])).toEqual(['tmp/ttl=14d', 'iris', 'tmp/ttl=7d'])
    expect(parsePaths([''])).toEqual([])
  })
})
