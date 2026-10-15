import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setRegistry } from './identities'
import { buildRegistry } from './identityRegistry'
import { canonId, ghHandle, selfAvatar, shortName, shortUserKey } from './UserChip'

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

describe("the signed-in user's own photo", () => {
  const PIC = 'https://lh3.googleusercontent.com/a/pic'
  it("is their chips' avatar, matched by canonical id, and no one else's", () => {
    const me = { email: 'ryan@runsascoded.com', avatar: PIC }
    expect([
      selfAvatar('ryan@runsascoded.com', me),
      selfAvatar('Ryan@RunsAsCoded.com', me),
      selfAvatar('ryan', me),
      selfAvatar('jane@example.org', me),
      selfAvatar('ryan', { email: 'ryan@runsascoded.com' }),
      selfAvatar('ryan', { ...me, guest: true }),
      selfAvatar('ryan', null),
    ]).toEqual([PIC, PIC, PIC, undefined, undefined, undefined, undefined])
  })
})
