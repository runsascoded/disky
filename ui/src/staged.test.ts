import { describe, expect, it } from 'vitest'
import { commonDir, elideMiddle } from './staged'

const D = '/Users/ryan/Downloads/'

describe('commonDir', () => {
  it('is the shared directory, on a segment boundary', () => {
    expect(commonDir([`${D}ChatGPT.dmg`, `${D}Photos-3-001.zip`, `${D}Photos-3-001/IMG_0001.jpg`])).toBe(D)
    // `Photos-3-001` and `Photos-3-001.zip` share characters, not a segment
    expect(commonDir([`${D}Photos-3-001`, `${D}Photos-3-001.zip`])).toBe(D)
  })
  it('never swallows an item that is itself the shared dir', () => {
    expect(commonDir([`${D}Photos-3-001`, `${D}Photos-3-001/IMG_0001.jpg`])).toBe(D)
    expect(commonDir([`${D}Photos-3-001/`, `${D}Photos-3-001/IMG_0001.jpg`])).toBe(D)
  })
  it('is the item\'s own dir for a single item, and empty when nothing is shared', () => {
    expect(commonDir([`${D}ChatGPT.dmg`])).toBe(D)
    expect(commonDir(['s3://a/x', 'gs://b/y'])).toBe('')
    expect(commonDir([])).toBe('')
  })
})

describe('elideMiddle', () => {
  const long = 'Insta360_Studio_6.0.4_release_insta360(RC_build71)_20260904_180546_signed_1788517496623.zip'
  it('keeps the head and the extension-bearing tail', () => {
    expect(elideMiddle(long)).toBe('Insta360_Studio_6.0.4_release_i…788517496623.zip')
    expect(elideMiddle(long).length).toBe(48)
  })
  it('leaves short names alone', () => {
    expect(elideMiddle('ChatGPT.dmg')).toBe('ChatGPT.dmg')
    expect(elideMiddle('x'.repeat(48))).toBe('x'.repeat(48))
  })
})
