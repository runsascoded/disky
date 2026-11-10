import { describe, expect, it } from 'vitest'
import { unownedBytes } from './types'
import { reaggregate } from './filterTree'

describe('ownership coverage', () => {
  it('recomputes unavailable ownership from the retained children', () => {
    const known = { n: 'known', b: 30, o: 1, us: [['alice', 20] as [string, number]] }
    const unknown = { n: 'unknown', b: 70, o: 1, ou: 50 }
    expect([
      reaggregate({ n: 'root', b: 100, o: 2, ou: 80 }, [known]),
      reaggregate({ n: 'root', b: 100, o: 2, ou: 80 }, [unknown]),
    ]).toEqual([
      { n: 'root', b: 30, o: 1, us: [['alice', 20]], d: undefined, c: [known] },
      { n: 'root', b: 70, o: 1, ou: 50, us: undefined, d: undefined, c: [unknown] },
    ])
  })

  it('distinguishes unknown bytes from known unowned bytes, including a partly attributed rollup', () => {
    expect([
      unownedBytes({ b: 100 }),
      unownedBytes({ b: 100, ou: 100 }),
      unownedBytes({ b: 100, us: [['alice', 30]], ou: 50 }),
      unownedBytes({ b: 100, us: [['alice', 100]] }),
    ]).toEqual([100, 0, 20, 0])
  })
})
