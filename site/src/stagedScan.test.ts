import { describe, expect, it } from 'vitest'
import { sizedScan } from './stagedScan'

const OLD = '2026-11-04T0703', NEW = '2026-11-05T0702', NEWER = '2026-11-06T0701'

describe('sizedScan: which scan a long-open /staged sizes at', () => {
  it('follows the newest when nothing was picked', () => {
    expect([sizedScan([NEW, OLD], null), sizedScan([NEWER, NEW, OLD], null)]).toEqual([NEW, NEWER])
  })
  it('keeps a picked scan as newer ones land', () => {
    expect(sizedScan([NEWER, NEW, OLD], OLD)).toBe(OLD)
  })
  it('a picked scan the list dropped falls back to the newest', () => {
    expect(sizedScan([NEWER, NEW], OLD)).toBe(NEWER)
  })
  it('no scans yet: none', () => {
    expect(sizedScan([], null)).toBe('')
  })
})
