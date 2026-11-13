import { describe, expect, it } from 'vitest'
import { flatRows } from './largest'

describe('flatRows', () => {
  it('names each listed path below the drilled dir; a stamp as its epoch day, a folder with its objects', () => {
    expect(flatRows('Users/ryan', [
      { path: 'Users/ryan/Downloads/a/big.iso', size: 4096, mtime: 86400 * 20000 + 5 },
      { path: 'Users/ryan/big.iso', size: 1024, mtime: null },
    ], 'file')).toEqual([
      { name: 'Downloads/a/big.iso', kind: 'file', bytes: 4096, objects: 1, mtime: 86400 * 20000 + 5 },
      { name: 'big.iso', kind: 'file', bytes: 1024, objects: 1 },
    ])
    expect(flatRows('', [{ path: 'Users/ryan', size: 5120, mtime: 86400 * 20001, n_files: 2 }], 'dir')).toEqual([
      { name: 'Users/ryan', kind: 'dir', bytes: 5120, objects: 2, mtime: 20001 * 86400 },
    ])
  })
})
