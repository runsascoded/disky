import { describe, expect, it } from 'vitest'
import { barControls } from './pageBar'

describe('barControls', () => {
  it('cw store (no attribution, no classes, no read logs): written / tree and the path filter only', () => {
    expect(barControls({ owners: false, hasAttr: false, classes: false, readRange: false })).toEqual({
      color: ['date', 'tree'],
      shade: false,
      classes: false,
      ownerFilter: false,
      pathFilter: true,
    })
  })
  it('gcs store with a rich scan: every control', () => {
    expect(barControls({ owners: true, hasAttr: true, classes: true, readRange: true })).toEqual({
      color: ['read', 'user', 'date', 'tree'],
      shade: true,
      classes: true,
      ownerFilter: true,
      pathFilter: true,
    })
  })
  it('gcs store, a scan without attribution or read logs: owner axes fall away, the rest stays', () => {
    expect(barControls({ owners: true, hasAttr: false, classes: true, readRange: false })).toEqual({
      color: ['date', 'tree'],
      shade: true,
      classes: true,
      ownerFilter: false,
      pathFilter: true,
    })
  })
})
