import { describe, expect, it } from 'vitest'
import { fmtBytesPrecise } from './types'

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
