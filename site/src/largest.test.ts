import { describe, expect, it } from 'vitest'
import { flatRows } from './largest'

describe('flatRows', () => {
  it('names each listed path below the drilled dir; a stamp as its epoch day, a folder with its objects', () => {
    expect(flatRows('Users/ryan', [
      { path: 'Users/ryan/Downloads/a/big.iso', size: 4096, mtime: 86400 * 20000 + 5 },
      { path: 'Users/ryan/big.iso', size: 1024, mtime: null },
    ], 'file')).toEqual([
      { n: 'Downloads/a/big.iso', k: 'file', b: 4096, o: 1, d: 20000 },
      { n: 'big.iso', k: 'file', b: 1024, o: 1 },
    ])
    expect(flatRows('', [{ path: 'Users/ryan', size: 5120, mtime: 86400 * 20001, n_files: 2 }], 'dir')).toEqual([
      { n: 'Users/ryan', k: 'dir', b: 5120, o: 2, d: 20001 },
    ])
  })
})
