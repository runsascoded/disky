import { describe, expect, it } from 'vitest'
import { unownedBytes } from './types'
import { reaggregate } from './filterTree'

describe('ownership coverage', () => {
  it('recomputes unavailable ownership from the retained children', () => {
    const known = { name: 'known', bytes: 30, objects: 1, owners: [['alice', 20] as [string, number]] }
    const unknown = { name: 'unknown', bytes: 70, objects: 1, unknown_owner_bytes: 50 }
    expect([
      reaggregate({ name: 'root', bytes: 100, objects: 2, unknown_owner_bytes: 80 }, [known]),
      reaggregate({ name: 'root', bytes: 100, objects: 2, unknown_owner_bytes: 80 }, [unknown]),
    ]).toEqual([
      { name: 'root', bytes: 30, objects: 1, owners: [['alice', 20]], mtime: undefined, children: [known] },
      { name: 'root', bytes: 70, objects: 1, unknown_owner_bytes: 50, owners: undefined, mtime: undefined, children: [unknown] },
    ])
  })

  it('distinguishes unknown bytes from known unowned bytes, including a partly attributed rollup', () => {
    expect([
      unownedBytes({ bytes: 100 }),
      unownedBytes({ bytes: 100, unknown_owner_bytes: 100 }),
      unownedBytes({ bytes: 100, owners: [['alice', 30]], unknown_owner_bytes: 50 }),
      unownedBytes({ bytes: 100, owners: [['alice', 100]] }),
    ]).toEqual([100, 0, 20, 0])
  })
})
