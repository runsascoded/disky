import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setRegistry } from './identities'
import { buildRegistry } from './identityRegistry'
import { canonId, ghHandle, shortName, shortUserKey } from './UserChip'

describe('the runtime registry', () => {
  beforeEach(() => setRegistry(buildRegistry([
    { u: 'jane-doe', aliases: ['jane', 'jd', 'jane.doe@example.org'], github: 'janedoe' },
    { u: 'lee-roe', aliases: [], name: 'Lee R' },
  ])))
  afterEach(() => setRegistry({}))

  it('shortUserKey is the shortest alias that prefixes the canonical id, never an unrelated handle', () => {
    expect(shortUserKey('jane-doe')).toBe('jane')
    expect(canonId('jd')).toBe('jane-doe')
    expect(shortUserKey('nobody-here')).toBe('nobody-here')
  })
  it('an email resolves through its sanitized local part', () => {
    expect(canonId('Jane.Doe@example.org')).toBe('jane-doe')
  })
  it('names: explicit, else the capitalized first segment; GitHub only when curated', () => {
    expect([shortName('jd'), shortName('lee-roe'), shortName('sam-poe')]).toEqual(['Jane', 'Lee R', 'Sam'])
    expect([ghHandle('jane'), ghHandle('lee-roe')]).toEqual(['janedoe', undefined])
  })
})

describe('no registry (a store that publishes no rules)', () => {
  it('ids stand as they are', () => {
    expect([canonId('jd'), shortName('jane-doe'), ghHandle('jane-doe')]).toEqual(['jd', 'Jane', undefined])
  })
})
