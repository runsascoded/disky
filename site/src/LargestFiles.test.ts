import { describe, expect, it } from 'vitest'
import { relParts } from './LargestFiles'

describe('relParts', () => {
  it('splits a listed file below the drilled dir into its dir (from the root), the part below the dir, and its basename', () => {
    expect([
      relParts(['Users', 'ryan'], 'Users/ryan/Downloads/a/big.iso'),
      relParts(['Users', 'ryan'], 'Users/ryan/big.iso'),
      relParts([], 'Users/big.iso'),
    ]).toEqual([
      { dirSegs: ['Users', 'ryan', 'Downloads', 'a'], rel: ['Downloads', 'a'], base: 'big.iso' },
      { dirSegs: ['Users', 'ryan'], rel: [], base: 'big.iso' },
      { dirSegs: ['Users'], rel: ['Users'], base: 'big.iso' },
    ])
  })
})
