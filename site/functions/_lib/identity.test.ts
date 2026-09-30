import { describe, expect, it } from 'vitest'
import { buildRegistry } from '../../src/identityRegistry.js'
import { canonId, whoToHandle } from './identity.js'

const reg = buildRegistry([
  { u: 'gonzalo-benegas', aliases: ['gonzalo', 'gonzalobenegas'], github: 'gonzalobenegas' },
])

describe('canonId', () => {
  it('maps a sign-in email to its registry id via the handle', () => {
    expect(whoToHandle('gonzalo.benegas@example.org')).toBe('gonzalo-benegas')
    expect(canonId('gonzalo.benegas@example.org', reg)).toBe('gonzalo-benegas')
  })
  it('follows alias keys to the canonical id', () => {
    expect(canonId('gonzalobenegas@example.org', reg)).toBe('gonzalo-benegas')
    expect(canonId('Gonzalo@example.org', reg)).toBe('gonzalo-benegas')
  })
  it('falls back to the sanitized handle for an unknown user (or no registry)', () => {
    expect(canonId('new.person+x@example.org', reg)).toBe('new-person-x')
    expect(canonId('gonzalobenegas@example.org')).toBe('gonzalobenegas')
  })
})
