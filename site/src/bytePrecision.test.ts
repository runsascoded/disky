import { describe, expect, it } from 'vitest'
import { fmtBytesPrecise, fmtBytesStep } from './types'

describe('run byte precision', () => {
  it('retains three significant figures across IEC scales', () => {
    expect([0, 12, 1.234 * 1024, 15.234 * 1024 ** 4, 50.049 * 1024 ** 4, 999.8 * 1024 ** 3].map(b => fmtBytesPrecise(b, 'iec')))
      .toEqual(['0', '12 B', '1.23 Ki', '15.2 Ti', '50.0 Ti', '1000 Gi'])
  })
  it('honors SI and the B-suffix preference', () => {
    expect([fmtBytesPrecise(15.234e12, 'si'), fmtBytesPrecise(50.049e12, 'si', true), fmtBytesPrecise(4.712 * 1024 ** 4, 'iec', true)])
      .toEqual(['15.2 T', '50.0 TB', '4.71 TiB'])
  })
})

describe('fmtBytesStep: a fitted axis labels ticks distinctly', () => {
  it('adds the decimals a narrow step needs, never fewer than the plain format', () => {
    const T = 1e12
    expect([7.40, 7.42, 7.44, 7.46].map(t => fmtBytesStep(t * T, 0.02 * T, 'si'))).toEqual(['7.40 T', '7.42 T', '7.44 T', '7.46 T'])
    expect([7.4, 7.5].map(t => fmtBytesStep(t * T, 0.1 * T, 'si'))).toEqual(['7.4 T', '7.5 T'])
    expect(fmtBytesStep(7.4021 * T, 0.0005 * T, 'si', true)).toBe('7.4021 TB')
    expect(fmtBytesStep(40 * T, 10 * T, 'si')).toBe('40 T')
    expect(fmtBytesStep(0, 1, 'iec')).toBe('0')
    expect(fmtBytesStep(5 * 1024 ** 4, 0, 'iec')).toBe('5.0 Ti')
  })
})
